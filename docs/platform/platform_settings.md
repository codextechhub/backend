# Platform Settings

## Purpose

Platform Settings is the platform-staff control surface for defaults and policies that apply across CodeX. It uses the same sectioned settings design as Finance and Procurement, but it does not duplicate school records, user management, workflow design, security operations, or other specialist consoles.

The first release exposes only values with a real backend consumer. Adding an arbitrary configuration definition does not make product behavior change, so a field is not considered implemented until runtime code reads it.

## Scope and permissions

The curated endpoints are:

- `GET/PATCH /v1/config/platform-settings/` for profile and onboarding;
- `GET/PATCH /v1/config/security-settings/` for runtime protection;
- `GET/PATCH /v1/config/integration-settings/` for safe delivery defaults and connection status.
- `POST /v1/config/integration-settings/test/` for safe SMTP and payment checks.

- Platform profile, onboarding, and integration controls remain platform-tenant operations.
- Security may be read or tightened at platform, school, or branch scope by an actor holding the dedicated permission at that authorized scope.
- `config.value.view` controls reading the profile and onboarding defaults.
- `config.value.update` separately controls saving them.
- `config.security.view` and `config.security.manage` separately control runtime security reading and saving.
- `config.integration.view` and `config.integration.manage` separately control integration reading and saving.
- `config.audit.export` controls filtered configuration-audit CSV export separately from audit viewing.
- Security and integration saves do not create approval requests. Possessing the dedicated manage permission is the authorization decision.
- The frontend renders a read-only form when the user can view but cannot update.
- The backend is authoritative. Hiding a Save button is not the permission boundary.

The generic configuration catalogue, capability controls, export, and audit endpoints keep their existing `config.*` permissions. The generic value API rejects security and integration keys, so `config.value.update` cannot bypass either dedicated manage permission.

## Storage and audit model

The first release reuses `vs_config` rather than adding another settings table:

- `ConfigurationDefinition` declares each key, type, validation rules, and allowed scope.
- `ConfigurationValue` stores a platform override.
- the curated endpoint validates the admin form and delegates writes to the same configuration service as the advanced catalogue;
- each changed field produces an immutable `ConfigurationAuditEvent` in the same database transaction;
- configuration audit is also mirrored to the shared audit stream on a best-effort basis;
- clearing an optional profile field deletes its platform override and records `config.value.cleared`.

The generic reset endpoint is `DELETE /v1/config/values/{key}/`. It deletes only the physical row at the resolved platform, tenant, or branch scope, then returns the newly effective value and its source. Repeating the reset is safe and reports that no override was present. Security and integration values use their dedicated PATCH endpoints with a field value of `null`, preserving their stronger permissions.

Profile, onboarding, and integration keys are platform-only. Security keys allow platform, school, and branch values under the compliance rules below.

Their schemas are product-owned. The generic definition endpoint cannot change their type, default, validation, allowed scope, sensitivity, active state, or archive them. Advanced catalogue shows that lock and omits Archive. This prevents an engineering-console action from breaking the curated forms or their runtime consumers.

## Platform profile

The profile contains:

| Field | Configuration key | Runtime consumer |
| --- | --- | --- |
| Platform name | `platform.profile.name` | Finance invoice and receipt issuer block |
| Tagline | `platform.profile.tagline` | Finance invoice and receipt issuer block |
| Address | `platform.profile.address` | Finance invoice and receipt issuer block |
| Email | `platform.profile.email` | Finance invoice and receipt issuer block |
| Phone | `platform.profile.phone` | Finance invoice and receipt issuer block |
| Website | `platform.profile.website` | Finance invoice and receipt issuer block |
| Logo URL | `platform.profile.logo_url` | Finance invoice and receipt issuer block |

Resolution order is:

1. a saved database value;
2. the matching `PLATFORM_ISSUER_*` deployment value;
3. an empty product fallback, except the Finance document still falls back to its ledger entity name and branch contact where that behavior already existed.

This preserves existing deployments while allowing an authorised platform admin to take ownership of the public issuer identity. Optional fields can be cleared to return to deployment fallback. Platform name is required when it is submitted.

The logo remains a public URL because the existing Finance renderer consumes a URL. File upload and media lifecycle management were deliberately not invented inside Settings.

## School onboarding defaults

The onboarding section contains:

| Field | Configuration key | Product default |
| --- | --- | --- |
| Ownership type | `platform.onboarding.default_ownership_type` | `PUBLIC` |
| Academic structure | `platform.onboarding.default_term_structure` | `3_TERMS` |
| Billing currency | `platform.onboarding.default_currency` | `NGN` |
| Branch country | `platform.onboarding.default_branch_country` | `Nigeria` |

These defaults apply only during creation:

- a new school receives ownership, academic structure, and currency when the request omits them;
- branches created inline with a school receive the branch-country default when omitted;
- a standalone branch created later receives the same country default when omitted;
- explicit request values always win;
- existing schools and branches are never rewritten when a default changes;
- school and branch update serializers do not read these defaults.

The allowed ownership, academic structure, and currency options come from backend model choices and are returned by the curated endpoint. The frontend does not maintain a second enum list.

## Runtime security and compliance inheritance

Runtime security is directly editable by a user with `config.security.manage` at the scope they are authorized to configure. There is no approval workflow. Every changed or reset field still creates an immutable configuration audit event.

| Field | Configuration key | Bounds | Runtime consumer |
| --- | --- | --- | --- |
| Failed login threshold | `security.failed_login_threshold` | 3 to 20 attempts | Account lockout registration during login |
| Account lock duration | `security.account_lock_minutes` | 5 to 1,440 minutes | Account lockout registration during login |
| Self-service reset lifetime | `security.self_reset_expiry_hours` | 1 to 24 hours | Self-service password reset creation |
| Admin reset lifetime | `security.admin_reset_expiry_hours` | 1 to 168 hours | Administrator-triggered password reset creation |
| Invitation lifetime | `security.invitation_expiry_days` | 1 to 30 days | Invitation creation and resend reset |
| Proxy idle timeout | `security.proxy_idle_timeout_minutes` | 5 to 120 minutes | Request authentication and stale-session sweeping |

Resolution is a saved database value followed by the safe product default. Runtime readers fail safely to the product default if configuration storage is temporarily unavailable. Changes affect newly evaluated attempts, links, invitations, and session checks. Existing reset-link and invitation expiry timestamps are not retroactively rewritten.

School and branch overrides follow branch, school, platform, then product-default inheritance. A child scope can only keep or strengthen its parent baseline:

| Field | Child-scope compliance direction |
| --- | --- |
| Failed login threshold | Same number or lower |
| Account lock duration | Same duration or higher |
| Self-service reset lifetime | Same duration or lower |
| Admin reset lifetime | Same duration or lower |
| Invitation lifetime | Same duration or lower |
| Proxy idle timeout | Same duration or lower |

The API returns each effective value, its winning source scope, whether the current scope owns a physical override, and the parent compliance boundary. Reset deletes only the current layer. A branch then follows its school; a school then follows the platform. The backend validates the boundary, so changing the request body cannot weaken the policy.

Login lockout, password-reset creation, invitation creation/resend, and request-time proxy expiry pass the user's tenant and branch to the resolver. The background stale-proxy sweep keeps the platform baseline as a cleanup pass; request authentication applies a stricter school or branch timeout before a proxy session can continue.

## Integrations and delivery

The integration section deliberately separates safe application defaults from deployment secrets.

Editable with `config.integration.manage`:

| Field | Configuration key | Runtime consumer |
| --- | --- | --- |
| Default sender name | `integrations.email.sender_name` | Central email From-header builder |
| Default sender address | `integrations.email.sender_address` | Central email From-header builder |
| Email retry budget | `notifications.email_max_retries` | Queued email delivery task |
| Email retry delay | `notifications.email_retry_backoff_seconds` | Queued email delivery task |

Sender identity falls back to `DEFAULT_FROM_EMAIL`. A message-specific display name, such as an inviter or password-reset administrator, still takes precedence while the configured sender address is preserved.

The same endpoint returns secret-free status for SMTP, Paystack, and the public application URL. It returns configured/not-configured state, provider name, SMTP host, and public URL only. SMTP passwords, provider keys, callback controls, and other credentials remain deployment-owned and are never serialized.

An integration manager may run two bounded connection tests without an approval request:

- SMTP opens and closes the configured Django email backend. It sends no message and accepts no host or recipient input.
- Payments performs the provider adapter's authenticated, read-only health check. Paystack uses its balance read endpoint but no balance data is returned or stored. It creates no charge, customer, recipient, or transfer.

Both actions use deployment-owned endpoints and credentials, are limited to one attempt per operator and connection every 30 seconds, and record `config.integration.connection_tested`. Responses contain only connected/failed wording. Provider response bodies, SMTP exceptions, secrets, and credential fragments are not returned.

## Inheritance reset and audit detail

Advanced configuration now exposes Reset when a physical value exists at the selected scope. Reset removes that row and reveals the next source in this order: branch, tenant, platform, definition default.

Audit improvements include:

- exact action and target-type filters;
- actor filter support;
- inclusive `created_after` and `created_before` filters;
- a scope-authorized detail endpoint for one event;
- a responsive detail drawer showing actor, reason, target, timestamp, and field-level before and after snapshots;
- redaction before storage for secret-reference values.
- discovered actor and target facets scoped to the same history the caller may read;
- exact target-ID filtering;
- a `config.audit.export` protected CSV export that honors the same scope, date, action, actor, target-type, and target filters as the screen;
- direct CSV download for filtered results up to 5,000 events;
- personal saved views for the current date window, action, actor, target, and authorized school scope;
- asynchronous CSV export when a filtered result exceeds 5,000 events;
- user-owned export jobs with a three-active-job limit, a 250,000-row safety cap, chunked generation, and a seven-day download window;
- exact filter snapshots on queued jobs, so later screen changes do not alter the requested export;
- download authorization that rechecks both the requesting user and the original tenant or branch scope.

Audit detail uses the same tenant and branch resolution as the list. Changing an event ID cannot escape the caller's authorized scope.

## Frontend sections

The Platform Settings navigation is:

1. Overview
2. Platform profile
3. School onboarding
4. Security
5. Integrations
6. Features and access
7. Administration
8. Audit and compliance
9. Advanced catalogue

The shared layout lives at `console-fe/src/components/settings/settings-layout.tsx`. Finance and Procurement import the same neutral component, so all three settings consoles retain one visual language and responsive behavior.

The Overview also shows enforced, non-editable safeguards so an admin can see what is protected without mistaking it for a missing setting:

- tenant and branch isolation;
- backend permission checks;
- immutable configuration audit.

Administration is a directory of permission-aware links to the dedicated team, role, workflow, communications, and security consoles. Those records remain owned by their modules.

Features and access preserves the capability catalogue, entitlements, dependencies, and scoped override controls, with the following additions:

- an entitlement may have an optional activation time and exclusive expiry time;
- future grants remain off until activation and expired grants resolve off immediately at evaluation time;
- schedules are evaluated on every access check, so no background activation job is required;
- denying an entitlement cannot carry schedule dates;
- expiry must be in the future and after activation when both are supplied;
- Reset to inherit deletes only the selected school entitlement, revealing the platform entitlement beneath it;
- every schedule change and inheritance reset records the dates in immutable audit snapshots;
- a 90-day renewal calendar combines platform and school grants and classifies expired, seven-day, 30-day, 90-day, and scheduled-activation entries;
- administrators can bulk-schedule up to 100 distinct platform or school entitlements in one atomic request;
- bulk scheduling locks existing rows, validates the complete batch before committing, preserves omitted dates, and writes one audit event per changed entitlement;
- expiry warnings appear in the settings dashboard. Email or external reminders are intentionally not sent until recipient ownership and retry policy are defined.

Effective capability list and configuration export evaluation now preload the catalogue, dependency edges, applicable entitlement layers, and applicable overrides. The number of database queries stays fixed as the catalogue grows instead of repeating queries for each capability and dependency.

Advanced catalogue preserves low-level typed definition and value administration for expert use. Each definition now includes a code-owned consumer label where a verified backend reader exists. The label names the service, concrete consumer, and runtime impact. Definitions without a registered consumer are explicitly marked, and administrators cannot edit ownership claims through the catalogue.

## Values intentionally kept outside Platform Settings

- School and branch identity records belong in School Management.
- Platform users and invitations belong in Team Management.
- Roles, assignments, permission groups, and permission dependencies belong in RBAC consoles.
- Approval ladders belong in Workflow templates.
- Notification templates and delivery activity belong in Communications.
- Sessions, login attempts, lockouts, and impersonations belong in Security Operations.
- Finance and Procurement entity policies remain in their module settings.
- Password hashing, token signing, trusted proxy behavior, rate limits, database connections, object storage, and secret values remain deployment or infrastructure controls.

## Adding another Platform Setting safely

Before exposing another field:

1. Identify every runtime consumer and the current fallback.
2. Decide whether the value is a platform default, a school override, or a branch override.
3. Confirm explicit transaction or onboarding input precedence.
4. Add a typed definition and migration.
5. Add the key to the curated product contract when it belongs in the main settings UI.
6. Apply it in the real backend consumer.
7. Keep the save and audit write transactional.
8. Add permission-denied, invalid-value, fallback, explicit-precedence, consumer, and audit tests.
9. Add the field to the existing sectioned design and verify desktop, tablet, and phone rendering against the real backend.

A catalogue-only key is not a complete setting.

## Important code locations

Backend:

- `apps/vs_config/platform_settings.py`
- `apps/vs_config/runtime_settings.py`
- `apps/vs_config/views.py`
- `apps/vs_config/serializers.py`
- `apps/vs_config/services/resolution.py`
- `apps/vs_config/services/capabilities.py`
- `apps/vs_config/services/audit_exports.py`
- `apps/vs_config/tasks.py`
- `apps/vs_config/migrations/0007_audit_views_exports_and_entitlement_indexes.py`
- `apps/vs_config/migrations/0004_seed_platform_settings.py`
- `apps/vs_config/migrations/0005_seed_runtime_settings.py`
- `apps/vs_finance/documents.py`
- `apps/schools/vs_schools/serializers.py`
- `apps/core/mail.py`
- `apps/vs_user/services/auth.py`
- `apps/vs_user/services/password.py`
- `apps/vs_user/services/invitation.py`
- `apps/vs_admin_console/services.py`
- `apps/vs_rbac/authentication.py`

Frontend:

- `src/pages/protected/settings/index.tsx`
- `src/redux/services/config-api.ts`
- `src/components/settings/settings-layout.tsx`
- `src/routes/protected/index.tsx`

## Future expansion

The next unblocked product work is:

1. Define recipients, escalation ownership, retry behavior, and quiet hours before adding email or in-product entitlement reminders.
2. Add renewal ownership and contract-reference fields if commercial teams need calendar assignment rather than platform-admin scheduling alone.
3. Add export cancellation and object-storage lifecycle monitoring if export volume makes operational controls necessary.
4. Expand the same code-owned consumer registry to platform profile and onboarding defaults.
5. Add cache-backed capability snapshots only after production catalogue size and request profiling show that the fixed-query evaluator is still material.

Future expansion requiring a product decision before implementation:

1. Additional payment providers after more than one production provider is genuinely supported.
2. Whether school security managers may configure their own baseline in the console, or whether platform security staff remain the sole operators targeting school and branch scopes.
3. Whether an expired commercial entitlement should notify only, disable immediately as it does now, or enter a contractual grace period.
4. File-managed platform branding and logo lifecycle instead of a public URL.

Still deployment-owned and not candidates for ordinary dashboard editing: secret keys, SMTP passwords, signing configuration, CORS and host policy, TLS/HSTS, database and Redis connections, storage credentials, broker configuration, and infrastructure probe targets.
