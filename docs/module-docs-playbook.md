# Module-docs playbook - how this initiative runs

The repeatable method behind `docs/finance/` (16 slice reports, July 2026). Any new
session continuing this work - **next: `vs_payments`, then `vs_procurement`** -
follows this file. It is the source of truth for process; per-slice content truth is
always the code.

## Mission & status

Document every backend module as **per-subject slice reports** so future programmers
can trace endpoints → calculations → output shapes without reading the code cold.

- ✅ `vs_finance` - complete: 16 slices in `docs/finance/`, every gotcha fixed or
  explicitly justified in each doc's §8.
- ✅ `vs_payments` - complete: 3 slices in `docs/payments/` - `payment_collections`
  (collections + virtual accounts), `payment_settlement` (payouts, batches,
  settlement reconciliation + movements/transactions feeds),
  `payment_webhooks_providers` (async webhook pipeline + OPay/Paystack/Fake
  adapters). Gotchas swept; full `vs_payments` app suite 139 green. The payout
  maker-checker item is **closed in code** by `681456f`: `vs_payments.provisioning`
  registers the payout ladder against finance's entity provisioning, so a new
  entity is gated from onboarding and seeded blocked (the stage never auto-skips
  and the
  `payout-approver` role starts with nobody in it). The residual is install-time
  only: an entity provisioned before that commit gets no ladder retroactively and
  needs a one-time `seed_payout_approvals --platform --all-tenants`.
- ✅ `vs_procurement` - complete: all 5 slices written:
  `procurement_master_data`, `procurement_sourcing`, `procurement_p2p_chain`,
  `procurement_inventory`, and `procurement_reports`. Every reports §8 decision
  is implemented or justified; full procurement QA is 263 green.
- 📝 `vs_user` - documented: 6 slices in `docs/user/` - `user_accounts`,
  `user_authentication`, `user_invitations_activation`, `user_passwords`,
  `user_organogram`, `user_security_monitoring`. Baseline at the time of writing:
  **102 tests, 1 error** - `SeedOrganogramCommandTests.test_seed_builds_tree_and_seats_staff`
  dies on `UnicodeEncodeError` because `seed_organogram` prints a `→` (U+2192) and
  a Windows cp1252 stream cannot encode it
  (`vs_user/management/commands/seed_organogram.py:195`). Environmental, not a
  logic failure, but it makes the suite red on Windows.
  **§8 gotchas are recorded but NOT yet swept** - the loop stopped at
  step 3 (docs committed) and steps 4-5 (briefing, fixes) are outstanding. The
  worst item: `/v1/user/auth-events/` declares `HasRBACPermission` but sets no
  `rbac_permission`, and identity audit rows are written with a null tenant
  against a manager configured `include_global=True`, so any authenticated user
  in any tenant can read the whole platform's identity audit trail.
- 📝 `vs_admin_console` - documented: 3 slices in `docs/console/` -
  `console_impersonation` (proxy sessions, the two permission namespaces, the
  auth-layer identity swap and the middleware access trail), `console_overview`
  (the one-request landing screen and its per-section gating), and
  `console_task_monitor` (the BackgroundJob engine-room views). Baseline at the
  time of writing: **104 tests, all green**.
  **§8 gotchas are recorded but NOT yet swept** - the loop stopped at
  step 3 (docs written) and steps 4-5 (briefing, fixes) are outstanding.
  The worst item: `ImpersonationSessionViewSet` is a plain `ModelViewSet`, so
  the router publishes `POST`/`PATCH`/`DELETE` on `/v1/admin/impersonations/`
  that fall through `get_permissions` to the **view** key, with every field of
  the session writable and `Model.clean()` never called. A School Admin holding
  the seeded `school.impersonation.view` can create an ACTIVE session targeting
  any active user in any tenant - a Vision Super Admin included - and ride it,
  with no justification, no tenant pinning and no audit bookend. Second: nine of
  the thirteen `console_overview` signals count every tenant's finance,
  procurement and payments documents, because those models are entity-scoped and
  `TenantAwareManager` never engages. Third: the task monitor is gated on
  Django's `is_staff` flag, which every CX staff account carries by
  construction, and exposes every tenant's job `result`, `error` and
  `traceback`.

- 📝 `vs_audit` - documented: 3 slices in `docs/audit/` - `audit_event_stream`
  (the central `AuditEvent` table, `emit_audit_event`, proxy attribution, the
  Event Explorer and the two `/me/` self-service routes), `audit_security_dashboard`
  (the one-request Security Dashboard aggregate), and `audit_compliance_exports`
  (compliance rules, the in-app CSV export, and the `audit.events` Export Centre
  dataset). Baseline at the time of writing: **10 tests, all green**.
  **§8 gotchas are recorded but NOT yet swept** - the loop stopped at
  step 3 (docs written) and steps 4-5 (briefing, fixes) are outstanding.
  The worst item is a hard bug: `POST /v1/audit/exports/` writes the entire CSV
  body into `AuditExportJob.file_path`, a `varchar(500)`, so any export past
  roughly three rows raises a PostgreSQL `DataError`, 500s, and leaves an
  orphaned `RUNNING` job row (no atomic block, `mark_failed` never called).
  Second: the Event Explorer has no tenant filter and `platform.audit.view` is
  seeded unrestricted/NORMAL, while nothing in the RBAC write path stops a
  `platform.*` key being attached to a school-tenant role - so a school admin
  holding `school.roles.create` can mint themselves a role that reads every
  tenant's audit trail, `metadata` included. Third: exporting the trail writes
  no audit event, though `EXPORT_REQUESTED`/`COMPLETED`/`FAILED` exist and are
  templated for it, and every holder of `platform.audit.export` can read every
  other holder's export file body. Fourth: only three of eighteen writer files
  pass `tenant=` to `emit_audit_event`, so most rows carry `tenant = NULL` -
  which is why the tenant-scoped Export Centre dataset returns almost nothing
  and why scoping the console is blocked until the column is backfilled.

- 📝 `vs_notifications` - documented: 3 slices in `docs/notifications/` -
  `notification_dispatch_engine` (`send_notification`, channel resolution,
  render + shared email layout, the Celery delivery task and the two delivery
  signals), `notification_feed_history` (the user's in-app feed, read state,
  route acknowledgement, and the admin delivery history log), and
  `notification_templates_settings` (the effective settings matrix and its
  overrides, template CRUD + live preview, the event-type catalogue, and the
  seed commands). Unlike the other modules, the §8 findings are collected in a
  dedicated fourth file, **`docs/notifications/notification_code_issues.md`**,
  which each slice points at instead of repeating.
  Baseline at the time of writing: **`Ran 85 tests in 380.453s` - OK**
  (`cd apps && DB_NAME=cx_notifslice2 ../cx/Scripts/python.exe manage.py test
  vs_notifications --settings=apps.settings.local --noinput`). The suite is slow
  because every test's `setUp` re-seeds 56 templates and 56 platform settings
  rows.
  **Findings are recorded but NOT yet swept** - the loop stopped at step 3
  (docs written) and steps 4-5 (briefing, fixes) are outstanding.
  The worst item: notification rows are stamped with the *initiating* tenant
  while the feed reads through `Notification.objects`, a `TenantAwareManager`,
  so a recipient in a different tenant never sees the row. Confirmed with a
  real caller: `vs_tickets` dispatches `ticket.created` with
  `tenant=ticket.tenant` to CX support staff on the platform tenant, so the
  agent's in-app feed and unread badge never show it - and under eager mode,
  which staging defaults to, the delivery task cannot find the row either, so
  the email is dropped too and the row stays `PENDING` forever. The same
  mis-filing lets a school admin read CX staff email addresses in their history
  log, and lets a school admin's settings toggle silence the CX support queue.
  Second: `acknowledge-route` is the one feed route using `all_objects`, so it
  can mark read a row the same user cannot see. Third: eight active in-app
  event types have no click destination because `_PREFIX_ROUTES` maps
  `finance.` where the registry keys those events `billing.`. Fourth:
  `Notification.metadata` is an unvalidated control surface - `attachments`
  reads any path in `default_storage`, `bcc` mails anyone.

- 📝 `vs_config` - documented: 4 slices in `docs/config/` -
  `config_settings_catalogue` (typed definitions, scoped values, the
  branch/tenant/platform precedence chain, `conf.get_config`),
  `config_platform_runtime_settings` (the three curated screens, the transitive
  security compliance clamp, the code-owned consumer map and the bounded
  connection test), `config_capabilities_entitlements` (the unified capability
  catalogue, dependency graph, entitlement/override split, the bulk evaluator,
  renewal calendar and bulk scheduling), and `config_audit_trail_exports` (the
  append-only `ConfigurationAuditEvent`, its double immutability guard, facets,
  saved views, the synchronous CSV and the queued background export).
  Following the `vs_notifications` precedent, the §8 findings live in a
  dedicated file, **`error/config/config_code_issues.md`**, which each slice
  points at instead of repeating.
  Baseline at the time of writing: **`Ran 61 tests in 94.867s` - OK**
  (`cd apps && DB_NAME=cx_configslice ../cx/Scripts/python.exe manage.py test
  vs_config --settings=apps.settings.local --noinput`). The one traceback in
  that run is `test_oversized_export_fails_with_the_size_limit_in_its_own_words`
  logging its own expected failure.
  **Findings are recorded but NOT yet swept** - the loop stopped at step 3
  (docs written) and steps 4-5 (briefing, fixes) are outstanding.
  The worst item is a hard bug: `ConfigurationAuditFilterSerializer` accepts a
  UUID for `?actor=` while the user primary key is a `BigAutoField`, so a
  well-formed UUID reaches the ORM and 500s the audit list, the CSV export and
  any saved view or queued export job carrying it - and a queued job fails with
  "narrow the filters", which cannot help. Second: `bulk_schedule_entitlements`
  forces `state = GRANTED` and `source = MANUAL` on every target, so extending
  an expiry across a list that includes a DENIED tenant hands that tenant the
  module and erases PACKAGE provenance on the rest. Third: no school role is
  ever granted a `config.*` permission anywhere in the repo, so
  `/v1/config/effective-capabilities/` - which `Capability`'s own docstring
  names as the frontend's source of truth - is a 403 for every school user out
  of the box, along with the security-settings clamp built and tested for
  schools. Fourth: `default_enabled` is ignored for every entitlement-gated
  capability, contradicting its own field docstring, so "off by default, opt in
  per branch" is not available for modules at all.

- 📝 `vs_exports` - documented: 5 slices in `docs/exports/` -
  `export_catalogue_datasets` (the `Field`/`FilterDef`/`Dataset` vocabulary, the
  registry the 19 datasets and 18 screens publish into from their own
  `AppConfig.ready`, the filter compiler and the "export what this table is
  showing" translation), `export_builder_definitions` (saved recipes, sharing,
  drafts, the estimate/sample loop, capability flags and quick export),
  `export_runs_and_files` (the run lifecycle and its frozen config, the engine,
  the writers, omissions and failure codes, files, download authorisation and
  the two sweepers), `export_schedules` (recurrence maths, the dispatcher,
  pause/resume and the failure counter), and `export_audit_analytics` (the two
  pipelines and the four headline metrics). Following the `vs_notifications`
  and `vs_config` precedent, the §8 findings live in a dedicated file,
  **`error/exports/export_code_issues.md`**, which each slice points at.
  Baseline at the time of writing: **NOT ESTABLISHED - the suite was not run to
  completion in the documenting session, so nothing below is backed by a
  `Ran N tests` line.** Establish it before trusting any coverage claim:
  `cd apps && DB_NAME=cx_exportslice ../cx/Scripts/python.exe manage.py test
  vs_exports --settings=apps.settings.local --noinput`. Three attempts failed
  for environmental reasons, and the traps are worth recording:
  (a) piping the run through `tail` reports **`tail`'s** exit status, so an
  `exit 0` there is meaningless and the `Ran N tests` line is discarded - always
  redirect to a file instead; (b) two later attempts were stopped before
  finishing, and both died still printing `Creating test database for alias
  'default'...` after nine minutes, so on this box the migration-and-seed setup
  alone outlasts a ten-minute foreground budget. Prior recorded evidence for
  this app is the step-0 sweep of 2026-08-16, which counted **vs_exports 152**
  green; the file has grown since, so expect more. The §11 coverage sections in
  each slice were written by reading `tests.py`, not by running it, and they say
  so. One partial run did complete and is worth carrying forward:
  `ScheduleOccurrenceTests`, `AnalyticsSafetyTests` and
  `CatalogueRegistrationTests` together gave **`Ran 19 tests in 0.586s` -
  FAILED (errors=2)**. The tests themselves take under a second; the nine
  minutes is entirely `Creating test database`. Both errors are one
  environmental cause - `Path.read_text()` with no `encoding=` meeting UTF-8
  source on a cp1252 box - and they take out the two guards that enforce the
  domain-neutrality rule (`export_code_issues` §17).
  **Findings are recorded but NOT yet swept** - the loop stopped at step 3
  (docs written) and steps 4-5 (briefing, fixes) are outstanding.
  The worst item is that the feature does not exist for the customer:
  `seed_exports_permissions` grants its fifteen keys to `xvs_super_admin` and
  `xvs_platform_admin` on the Codex tenant only and writes no
  `PrebuiltRolePermission` row, so every Export Centre route is a 403 for every
  school user out of the box - the same shape as the `vs_config` finding, but on
  a user-facing feature. Second: archiving an export sets `is_archived` and
  nothing else reads it, so an archived export can still be run by id and its
  schedule keeps producing a file every night, invisibly. Third:
  `platform.schools` deliberately ignores its scope and returns
  `School.objects.all()`, and its key is an unrestricted `platform.*` NORMAL key
  that nothing stops a school role being given. Fourth: run references are
  `secrets.token_hex(3)` - 16.7M values, globally unique, allocated once with no
  retry - so collisions start around 4,800 runs, surface to the user as "A
  record with these details already exists", and inside the schedule dispatcher
  escape a loop that catches only `ExportServiceError`, killing the whole tick.
  There is **no exports FRD folder** under `docs/frd/functional-requirements/`;
  it was not created, per the standing rule that a missing module FRD is
  reported rather than generated.

- 📝 `vs_health` - documented: 4 slices in `docs/health/` -
  `health_signal_collection` (the timing middleware, the in-process buffer,
  `RequestMetric` and its latency histogram, the percentile maths, the golden
  signals, endpoint and tenant analytics), `health_uptime_availability` (the
  service registry, the five probe types, raw results and daily rollups, SLOs,
  the service grid and the posture banner), `health_incidents_alerts` (alert
  rules, the per-minute evaluator, auto-incidents, the war-room timeline,
  reliability stats and deployment annotations), and `health_queues_jobs` (the
  per-minute queue snapshot, the depth trend, the `celery` service card and the
  task table over `core.BackgroundJob`). Following the `vs_notifications`,
  `vs_config` and `vs_exports` precedent, the §8 findings live in a dedicated
  file, **`error/health/health_code_issues.md`**, which each slice points at.
  Baseline at the time of writing: **`Ran 27 tests in 2.139s` - OK**
  (`cd apps && DB_NAME=cx_healthslice ../cx/Scripts/python.exe manage.py test
  vs_health --settings=apps.settings.local --noinput`). The suite is fast
  because it is small: 27 tests over an app of ~3,400 lines, with no coverage
  at all for the probes, the queue snapshot, the task table, or nineteen of the
  twenty endpoints.
  **Findings are recorded but NOT yet swept** - the loop stopped at step 3
  (docs written) and steps 4-5 (briefing, fixes) are outstanding.
  Four findings were **confirmed by execution** against a real JWT in a
  throwaway test module that was deleted afterwards; the rest are traced to
  file and line.
  The worst item: `/v1/health/tasks/` cannot be reached by any request. Two
  things want the query parameter named `tenant` and want incompatible values -
  the auth layer requires a slug (`vs_rbac/authentication.py:95-112`) and
  `TaskListView` feeds that slug into `filter(tenant_id=…)` against a numeric
  primary key (`views.py:247-249`) - so `?tenant=codex` is a 500, `?tenant=all`
  and `?tenant=1` are 404s, and omitting it is a 400. The same collision leaves
  the tenant filter on the Command Center, API & Endpoint Health and Tenant
  Health screens permanently inert, because `_tenant_id` swallows the
  `ValueError` and widens to global. Second: nothing is ever alerted -
  `AlertRule.channel` is seeded with "PagerDuty", "Slack #sre" and "Zoho Cliq"
  and read by no code, so a SEV1 incident opens silently and waits for someone
  to open the screen. Third: `duration_sec` ("sustained for") is not
  implemented, so a metric hovering at its threshold opens and resolves a fresh
  incident every minute - and that flapping reaches `INC-10000` within a week,
  at which point the string-sorted code allocator (`tasks.py:221-230`) returns
  a duplicate and the `IntegrityError` kills the alert engine permanently.
  Fourth: three seeded probes point at `/v1/` and `/v1/payments/` (which do not
  resolve) and `/v1/user/` (which refuses anonymous callers), so `api`, `auth`
  and `payments` sit at WARNING forever and the posture banner reads
  "3 services degraded" on a healthy platform - which then feeds the rollup's
  treatment of WARNING as downtime into a permanently breached SLO and a
  permanently open incident.
  Worth recording as a strength: `platform.health.view` / `.manage` are
  declared `PermissionScope.PLATFORM` at creation (`seed.py:71`), so unlike
  `platform.schools` in `vs_exports` and `platform.audit.view` in `vs_audit`,
  the cross-tenant aggregates here genuinely cannot be reached from a school
  role. The residual is that neither key is granted to `xvs_platform_admin`, so
  in practice only the super admin can open the console.
  There is **no health FRD folder** under `docs/frd/functional-requirements/`;
  it was not created, per the standing rule that a missing module FRD is
  reported rather than generated.

## The loop (per slice)

1. **Trace the real code** - models, service functions, views (rbac keys + request
   bodies actually read), serializers (exposed fields, FLS), URLs, enums, seeds.
   Never write from memory of "how it should work".
2. **Write the doc** from `docs/finance/_report_template.md` (11 sections). The three
   sections that catch recurring errors: §3 *only the fields the view actually
   reads*, §5 *formula → the function that computes it*, §6 *what survives posting*.
   Cite `file:line` on every calc/posting/field claim. §8 lists gotchas honestly.
3. **Commit the doc** (docs-only commit, message style `docs(payments): …`).
4. **Gotcha briefing** - explain every §8 item to the user in *simple, non-technical
   terms*, sorted into: recommend-fix / judgment call / justified-by-design, each
   with a one-line verdict. Obvious wrong-money/crash bugs: fix immediately without
   asking. Everything else: wait for the user's picks.
5. **Fixes** - see Working mode below. After fixes land: flip the doc's §8 items to
   ✅ with the how, update `todo.md` (Done entry), commit.

## Working mode (conductor)

- **Fable (the main session) never writes feature code.** It orchestrates, briefs,
  and QAs. Docs, analysis, todo/memory edits, and git commits are Fable's domain.
- **Code changes go to an Opus-high subagent** via the Agent tool with a meticulous
  brief: exact files, behaviors, guards, migration expectations, named tests, test
  command, "DO NOT COMMIT", and a required report format. Use ONE sequential agent
  whenever fixes share `constants.py` or the migrations directory (parallel agents
  collide on migration numbering).
- **QA on return** (non-negotiable): `git status --short` must match the brief;
  review risky hunks line-by-line; run the full suite YOURSELF (don't trust the
  agent's line); check `makemigrations --check --dry-run` and re-run seeds. Defects
  go back to the same agent via SendMessage with a precise correction. Only then
  sync docs and commit.
- Bulk/token-heavy chores (computer use, mass analysis) may go to cheaper models.

## Conventions that bit us (learn once)

- **Stage files explicitly - never `git add -A`** (it once swept the user's
  unrelated in-progress work into a commit).
- Commit **directly to `main`**, do **not push** (user pushes). Trailer:
  `Co-Authored-By:` line per the harness rules.
- Tests: from `apps/`, `python manage.py test <targets>
  --settings=apps.settings.local --noinput` (Postgres). **Never run two test
  processes concurrently** (shared test DB → phantom failures). Suite baseline at
  handoff: **282 green** (`vs_finance core`); payments/procurement have their own
  tests - establish their baseline first.
- Money is integer kobo everywhere. Pagination is the `XVSPagination`
  `{pagination, data}` envelope (page 25, `?page_size=` ≤ 100). Response-shape
  changes are **frontend-visible** - always flag them in the report/commit.
- RBAC: every view has an `rbac_permission`; keys live in per-app
  `seed_*_permissions.py`; canonical verbs in `core/…/seed_actions.py`. New
  documents get their **own resource** (see the pettycash/salary splits). FLS
  (`FieldSecurityMixin.read_permissions`) masks sensitive fields - payments already
  uses `payments.payout.view_sensitive` (beneficiary masking in the movements feed).
- The finance posting engine is the reference for money-touching QA: balanced-or-
  rejected, closed-period guards, corrections by reversal (never edit posted
  history), audit row in the same commit, durable rejection rows.
- `todo.md` at repo root: Undone/Done ledger - add fix batches to Done with detail.

## Session-start checklist for the next session

1. Read this file + `docs/finance/_report_template.md`; skim one finished example
   (`docs/finance/finance_banking_reconciliation.md` is the richest).
2. `git log --oneline -10` and `git status` - note any user commits since; if the
   user changed vs_payments/vs_procurement, study those commits first (the user
   sometimes asks "study my commit and sync docs").
3. Establish the module's test baseline (`python manage.py test vs_payments
   --settings=apps.settings.local --noinput`).
4. Start with `payment_collections`, one slice at a time, per the loop above.
