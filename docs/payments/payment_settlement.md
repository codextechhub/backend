# payment_settlement - payouts, batches, reconciliation & the money feeds

> Slice 2 of `vs_payments`. Covers **money-out** and the **read-side money views**:
> the `PayoutInstruction` (one transfer line), the `PayoutBatch` (the mandatory
> approval envelope for single and bulk disbursement), the settlement-reconciliation report (gateway vs. bank),
> the unified movements feed, and the transactions log. Collections + virtual
> accounts are slice 1 (`payment_collections`); webhook ingestion + the PSP
> adapters are slice 3 (`payment_webhooks_providers`).

---

## 1. What it is (and what it is NOT)

A *payout* is a request to push money **out** of the entity to a beneficiary
through a PSP - the disbursement mirror of a collection. A `PayoutInstruction` is
the gateway record of that request; the authoritative money movement is a
**`vs_procurement.VendorPayment`** (Dr AP, Cr bank, Cr WHT), booked **only on
confirmation** (`services.py:632`, `_book_vendor_payment` at `services.py:658-688`).

A *payout batch* is the only provider-bound envelope. A bulk run holds many
instructions; `POST /payouts/` creates the same object with exactly one instruction.
Every shape is approval-gated through `vs_workflow` (maker-checker). The template
arrives **with the tenant's books**:
`provision_payout_approval` is registered against finance's entity provisioning
from `VsPaymentsConfig.ready()` (`provisioning.py:15-30`, `apps.py:20-27`) and runs
inside the transaction that creates the entity (`vs_finance/serializers.py:167-183`).
Entities provisioned before that commit are the one exception (§8.1).

The *settlement reconciliation*, *movements feed*, and *transactions log* are
**read-only** reports over the gateway records (and, for reconciliation, the
imported bank statement).

This does **NOT**:
- move money itself - the PSP does; we book the ledger mirror after confirmation.
- book anything at `initiate`/`submit` time - a `PENDING`/`PROCESSING` payout has
  no `vendor_payment_id` (`services.py:404-414`).
- **write** during reconciliation - it never mutates a bank line or books a
  journal; matching is advisory (`reconciliation.py:16`).
- leave money-out ungated when a template is absent - request creation rolls back,
  direct batch submission returns a typed 409, and `initiate_payout` is an explicit
  refusal. Nothing calls a provider until the exact batch has terminal human approval.
- ignore a provider-reported settled amount - like collections, `confirm_payout`
  now adopts the PSP's settled figure when it differs (see §5/§8.3).

## 2. Domain model

### `PayoutInstruction` - `models.py:259-330`
One request to send money out. Money is integer **kobo** (`amount`).
- `entity` (PROTECT) - tenant scope; `batch` → `PayoutBatch` (nullable, the bulk
  envelope this belongs to).
- `reference` - our merchant reference / idempotency key, `unique`
  (`CXP-<tenant_id><YYMMDD><daily_sequence>`, allocated per tenant/local day);
  `provider_reference` / `recipient_code` - the PSP's ids (`services.py:43-49`).
- `provider`, `amount`, `currency`.
- `beneficiary_name`, `beneficiary_account_number` (**both FLS-masked** - PII, §9),
  `beneficiary_bank_code`.
- `source_account` → `vs_finance.Account` (nullable - the bank/cash GL the booked
  payout credits; falls back to `1100`).
- `status` (`PayoutStatus`, default `PENDING`): `PENDING → PROCESSING → PAID |
  FAILED | REVERSED` (`constants.py:67-75`); terminal set `{PAID, FAILED,
  REVERSED}` (`constants.py:78-81`); `is_terminal` at `models.py:326-329`.
- **Loose ledger link (no hard FK into procurement):** `vendor_source_type` /
  `vendor_source_id` (the `Vendor` pk as a string) + `vendor_payment_id` (the
  booked `VendorPayment` pk), `models.py:303-306`.
- `failure_reason`, `metadata` (carries `wht_amount`), `raw_response`,
  `confirmed_at`, `created_by`.
- Indexes `(entity, status)`, `(provider, provider_reference)`.

### `PayoutBatch` - `models.py:176-257`
A bulk-disbursement envelope grouping many instructions.
- `entity` (PROTECT), `provider`, `reference` (`unique`), `title`, `narration`.
- `idempotency_key` plus `request_fingerprint` bind a tenant-scoped request key to
  one normalized payload. An exact replay returns the original batch; changed data
  under the same key returns `PAYOUT_IDEMPOTENCY_CONFLICT`.
- `status` (`PayoutBatchStatus`, default `DRAFT`): `DRAFT → PROCESSING → COMPLETED
  | PARTIALLY_COMPLETED | FAILED` (`constants.py:84-105`); terminal set
  `{COMPLETED, PARTIALLY_COMPLETED, FAILED}`.
- `total_amount` / `item_count` - **denormalised** sums of the children, kept in
  sync by the service (`services.py:519-522`).
- `source_account` (default bank/cash GL for the children), `currency`,
  `submitted_at`, `metadata` (carries `approval_status`), `created_by`.
- **Workflow bridge:** `workflow_document_type = "payments.payout_batch"`; the
  entity supplies the tenant used by the approval engine and `branch` is `None`.

### `PaymentEvent` - `models.py:381-421`
Append-only, immutable gateway action log (the transactions log). `save()` on an
existing pk and `delete()` both raise `ValueError` (`models.py:412-418`). `entity`
is **nullable** - and webhook-received/rejected events are written with no entity
(§8). Carries `action` (`PaymentAuditAction`), `provider`, `reference`,
`succeeded`, `message`, `metadata`, `actor_user`.

## 3. Endpoint map

Base `/v1/payments/`; all require `?entity=<id|code>`, platform envelope + RBAC.

| Method + path | permission key | what it does | request body (fields actually read) | response shape |
|---|---|---|---|---|
| `GET /payouts/` | `payments.payout.view` | list instructions, paginated | query: `group` (PENDING/PAID/FAILED), `status`, `provider` | `{pagination, data:[PayoutInstructionSerializer]}` |
| `POST /payouts/` | `payments.payout.create` | create a one-line batch and submit it for approval; never calls the PSP | `Idempotency-Key` header; `amount`(kobo,>0), `vendor`**, `source_account`, `provider`, `narration`, `wht_amount`, `metadata`; legacy beneficiary fields may be supplied only when they match the vendor master | `success_response(data=PayoutInstructionSerializer + approval, 201; exact replay 200)` |
| `GET /payouts/summary/` | `payments.payout.view` | KPI totals (settled 7d, pending, failed) + group counts | query: `provider` | `success_response(data={total, settled7d, pending, failed, group_counts})` |
| `GET /payout-batches/` | `payments.payout.view` | list batches (summary serializer, no child array) | query: `status` | `{pagination, data:[PayoutBatchSummarySerializer]}` |
| `POST /payout-batches/` | `payments.payout.create` | assemble a DRAFT batch + children; `submit:true` submits for approval and never dispatches directly | `Idempotency-Key` header; `items:[{amount, vendor**, narration, wht_amount, metadata}]`, `source_account`, `provider`, `title`, `narration`, `submit` | `success_response(data=PayoutBatchSerializer, 201; exact replay 200)` |
| `GET /payout-batches/summary/` | `payments.payout.view` | batch KPI totals (queued, completed7d, drafts) | - | `success_response(data={total, queued, completed7d, drafts})` |
| `GET /payout-batches/<pk>/` | `payments.payout.view` | one batch **with** its child instructions | - | `success_response(data=PayoutBatchSerializer)` |
| `POST /payout-batches/<pk>/` | `payments.payout.create` | retired direct provider route, always refused | - | `409 PAYOUT_APPROVAL_REQUIRED` |
| `POST /payout-batches/<pk>/submit-for-approval/` | `payments.payout_batch.submit` | route the batch through the vs_workflow approval engine | - | `success_response(data=PayoutBatchSerializer)` |
| `GET /reports/settlement-reconciliation/` | `payments.report.view` | gateway-confirmed movements vs. imported bank lines | query: `start_date`, `end_date` (ISO, inclusive), `provider` | `success_response(data={…, summary, rows[], unmatched_bank_lines[]})` |
| `GET /transactions/` | `payments.report.view` | the append-only gateway action log | query: `action`, `provider`, `succeeded` | `{pagination, data:[PaymentEventSerializer]}` |
| `GET /movements/` | `payments.report.view` | unified in+out feed, newest first; payout PII FLS-masked | query: `direction` (in/out), `group`, `provider` | `{pagination, data:[row]}` |
| `GET /movements/summary/` | `payments.report.view` | in7d / out7d / pending / failed across both gateways | query: `provider` | `success_response(data={in7d, out7d, pending, failed})` |

** = required. Notes:
- `amount` must be positive. `vendor` resolves **within the entity** by code or pk;
  a missing or foreign vendor is a 400. It must also be active, off hold, KYC
  VERIFIED, and have account name, account number, and provider bank code.
- Both creation routes require `Idempotency-Key`. A missing key is 400; the same
  key and same normalized request replay safely; a changed request is typed 409.
- **Approval-gate approve/reject/return** are driven through the **vs_workflow**
  action endpoints (not vs_payments URLs); the handler's `on_approved` calls
  `submit_payout_batch`. Who may vote is **role membership**, not a payments
  permission key: the seeded stage is ROLE-sourced on `payout-approver` and the
  engine guards those votes by eligibility in its service layer (§9).

## 4. Lifecycle / state machine

### Single payout, represented as a one-line batch
```
POST /payouts/              checker approval             provider accepts       confirmation
PENDING in DRAFT batch ──► terminal APPROVED batch ──► PROCESSING ──────────► PAID
       no provider call          exact workflow checked                         books vendor payment
```
The endpoint creates a one-line batch, submits it to workflow, and returns the
instruction plus its approval state. `initiate_payout` always raises
`PayoutApprovalRequiredError`, preventing stale workers or integrations from
reviving the former standalone cash-out route (`services.py:400-413`).
Confirmation funnels through `confirm_payout` (`services.py:595-648`), idempotent
via `select_for_update` + terminal short-circuit. A webhook triggers
`confirm_payout(payout)` with **no** status, so it **re-verifies** against the PSP
(`webhooks.py:178`; see §8/slice 3) rather than trusting the event.

### Payout batch
```
create (DRAFT)                submit-for-approval            required stages APPROVE
DRAFT ─────────► DRAFT + meta.approval_status=PENDING_APPROVAL ─────────► on_approved:
  (direct submit 409)          (no provider dispatch)            submit_payout_batch → PROCESSING
                                     │ REJECT/RETURN → meta.approval_status=DRAFT
```
The batch `status` stays `DRAFT` throughout approval; the phase lives in
`metadata["approval_status"]` (`workflow_handlers.py:62-68`). `validate_document`
preflights (must be a DRAFT batch with ≥1 PENDING child, `workflow_handlers.py:74-86`).
`on_approved` row-locks and passes the exact instance to `submit_payout_batch`.
The provider boundary then rechecks the instance-to-batch/tenant match, terminal
status, distinct human actors, locked item count and total, entity/provider
consistency, and the live verified vendor destination before the first PSP call.

## 5. Calculations

**Payout net & journal split** (`_book_vendor_payment`, `services.py:658-688` →
`vs_procurement/payables.py:381-563`), all kobo:
- `gross = payout.amount` - where `confirm_payout` first adopts the PSP's settled
  amount when it reports one that differs (`settled = amount or payout.amount`,
  keeping the original in `metadata["instructed_amount"]`, `services.py:626-630`);
  `wht = metadata["wht_amount"]` (default 0);
  `net = gross − wht` (`payables.py:469`). Guard: `0 ≤ wht ≤ gross` else
  `PostingError` (`payables.py:467`). Example: instructed `70 000`, `wht = 7 000`
  → net `63 000`.

**Batch totals** - `total_amount = Σ child amounts`, `item_count = len(items)`,
computed once at assembly (`services.py:519-522`); not recomputed on child failure
(a FAILED child still counts toward `total_amount`).

**Reconciliation, signed kobo** (`reconciliation.py`):
- Gateway sign: collection `+amount`, payout `−amount` (`reconciliation.py:174,188`);
  bank line `amount` is already signed (+in/−out). A correct pairing nets to zero.
- Matching is two-pass: **reference** first (our ref or the PSP ref), then an exact
  signed-**amount** fallback that picks the **date-nearest** bank line among equal
  amounts (`_closest`, `reconciliation.py:219-241`; see §8.4).
- `fee_amount = |gateway amount| − |settled bank amount|` - the PSP fee
  (`reconciliation.py:57-61`). Example: gross `40 000` settles to a `39 100` bank
  line → fee `900`.
- `is_reconciled` iff `unsettled_count == 0 and no unmatched bank lines`
  (`reconciliation.py:141-145`).

**Movements/summary** - 7-day money-in = `Σ collection.amount where status=SUCCEEDED
and confirmed_at ≥ now−7d`; money-out likewise for `status=PAID`
(`views.py:1011-1020`).

## 6. What posting does to the ledger

Only a **PAID** payout posts. `_book_vendor_payment` builds a draft
`vs_procurement.VendorPayment` and calls `post_vendor_payment`
(`payables.py:352-563`). Journal (source `BANK`), for gross `G`, WHT `W`,
net `N = G − W`:

| Dr / Cr | account | amount |
|---|---|---|
| **Dr** | vendor AP control (`vendor.payable_account`) | `G` |
| **Cr** | `source_account` (else fallback `1100` Cash & bank) | `N` |
| **Cr** | WHT payable (tax-code `collected_account`, else `WHT_PAYABLE_CODE`) | `W` (only if > 0) |

Carried vs dropped:
- `amount → gross_amount`, `metadata.wht_amount → wht_amount`, `currency`,
  `reference`, `narration`, `source_account → payment_account` all carry onto the
  `VendorPayment` (`services.py:671-679`).
- **Vendor is required to post.** `_book_vendor_payment` raises `PaymentStateError`
  if `vendor_source_id` is empty (`services.py:660-663`); it re-resolves the
  `Vendor` from the stored pk. A vendor `on_hold` blocks posting at the procurement
  layer (`payables.py:449`).
- **Auto-allocation to bills.** `post_vendor_payment` runs with its default
  `auto_allocate=True` (`_book_vendor_payment` passes no flag), so the gateway
  payout **settles the vendor's oldest open bills** (`payables.py:480-486`,
  `allocate_vendor_payment`). Parallel to the collections auto-allocation, but for
  a vendor payment this is the intended AP behaviour (§8).
- A `PayoutBatch` posts **nothing itself** - each *child* posts its own
  `VendorPayment` on confirmation.

## 7. Worked example

Bright Star's payment maker, Chinedu, queues a ₦600,000 vendor payout with
`Idempotency-Key: august-generator-01`. The API creates one DRAFT batch and one
PENDING instruction, then returns the active approval. Paystack has not been called.

1. Chinedu cannot approve the request he created.
2. Ada, who holds `payout-approver`, approves the checker stage. Because
   ₦600,000 is at or above the ₦500,000 threshold, the batch still cannot leave.
3. Bisi, a different person who holds `payout-senior-approver`, approves the
   senior stage. Only now does the handler pass the exact terminal instance to
   the provider boundary and the payout becomes PROCESSING.
4. If the bank destination changed while the request waited, dispatch fails
   before Paystack is called. Bright Star must verify the new account and approve
   a newly created batch.
5. If Chinedu retries the original HTTP request with the same key and payload,
   the original instruction and workflow return. If he changes the amount under
   that key, the API returns `PAYOUT_IDEMPOTENCY_CONFLICT` instead of duplicating
   or silently changing the payment.

## 8. Gotchas / known limitations

> The 25 August 2026 hardening closes the direct single-payout and unseeded batch
> bypasses. Items 2 through 7 below are earlier completed hardening work.

1. ✅ **Every payout now fails closed behind human approval.** Single requests are
   one-line batches. Direct detail submission and the legacy `initiate_payout`
   service refuse; missing templates roll back creation; and the payout handler
   forbids workflow's continue-without-approval release. The seeded ladder has an
   always-on `payout-approver` stage and a conditional
   `payout-senior-approver` stage at 50,000,000 kobo. Both park when unstaffed.
   Migration `0005_upgrade_seeded_payout_ladders` upgrades only the exact former
   shipped one-stage template, leaving administrator-authored ladders untouched.
   The provider boundary remains the final control even for custom ladders: one
   distinct human is mandatory below the threshold, two distinct humans are
   mandatory at or above it, and neither may be the requester.

   **Residual operational step.** Older tenants that have no payout template must
   run `python manage.py seed_payout_approvals --platform --all-tenants`. Until
   they do, payout creation fails closed with `TEMPLATE_NOT_FOUND`; it cannot fall
   back to provider dispatch. Existing custom ladders are not overwritten and
   should be reviewed and staffed by an administrator.

2. ✅ **Webhook events now carry the matched record's entity.** `ingest_webhook`
   resolves the target collection/payout once (`_find_record`) and passes its
   `entity` into the `WEBHOOK_RECEIVED` `PaymentEvent` (`webhooks.py:89-95`), so
   webhook actions appear in that entity's transactions log. The bad-signature
   `WEBHOOK_REJECTED` row intentionally stays `entity=NULL` (the payload is
   untrusted, so no entity can be attributed). Test:
   `test_webhook_received_event_is_attributed_to_the_entity`.

3. ✅ **Payouts now adopt the provider-reported settled amount.** `TransferResult`
   carries `amount` (`providers/base.py:74`, populated by Paystack/OPay/Fake);
   `confirm_payout` computes `settled = amount or payout.amount` and, when the PSP
   reports a positive figure that differs, stashes `metadata["instructed_amount"]`
   and books the settled gross (`services.py:611,626-630`). A `0` report never
   overrides. Tests: `test_payout_adopts_provider_settled_amount`,
   `test_confirm_payout_status_without_amount_keeps_instructed`.

4. ✅ **Amount-fallback now matches the date-nearest bank line.** Pass 2 no longer
   takes the first unconsumed same-amount line; `_closest` picks the candidate whose
   `txn_date` is nearest the gateway row's confirmation, preferring on/after
   confirmation, then smallest day-distance, then lowest id (`reconciliation.py:219-241`).
   Deterministic and order-independent for well-separated dates - still an advisory
   heuristic (no global optimum), and the console still flags `match_basis ==
   "amount"` rows for a human. Test:
   `test_amount_match_prefers_the_date_nearest_bank_line`. (Pass 1 reference matching
   is unchanged.)

5. ✅ **`fee_amount` is clamped at zero.** `max(0, |gross| − |settled|)`
   (`reconciliation.py:61`), so an over-settlement / reversal never displays a
   negative fee. Test: `test_over_settlement_fee_is_clamped_to_zero`.

6. ✅ **The "queued" KPI now counts only in-flight children.**
   `PayoutBatchSummaryView` sums child `PayoutInstruction` amounts where
   `batch is not null and status in (PENDING, PROCESSING)` instead of the batches'
   denormalised `total_amount` (`views.py:578-582`), so a FAILED child no longer
   inflates money-in-flight. (`total_amount` itself remains the assembly sum - the
   batch's face value.) Test:
   `test_payout_batch_summary_queued_counts_only_in_flight_children`.

7. ✅ **Movements feed no longer exposes internal ledger ids.** `linked_id` (the
   `payment_id` / `vendor_payment_id`) was dropped from the projection
   (`views.py:907-937`); `party` + `beneficiary_account` stay FLS-masked and
   `narration` is intentionally kept. Test: `test_movements_feed_hides_internal_linked_id`.

## 9. Permissions & tenant isolation

Keys (`seed_payments_permissions.py:27-48`), granted to `xvs_super_admin` /
`xvs_platform_admin`:
- `payments.payout.view` (NORMAL) - list/detail/summary/batches.
- `payments.payout.create` (**CRITICAL**) - create single or bulk payout requests;
  it never authorizes provider dispatch.
- `payments.payout.view_sensitive` (SENSITIVE) - unmask beneficiary name, account,
  and bank code
  (serializer FLS + movements masking).
- `payments.report.view` (NORMAL) - reconciliation, transactions, movements.
- `payments.payout_batch.submit` (SENSITIVE) - route a batch for approval
  (`views.py:651`).
- `payments.payout_batch.approve` / `.approve_high_value` (**CRITICAL**) remain
  seeded compatibility keys, but vote eligibility comes from the workflow's frozen
  role resolution: `payout-approver`, and for high value,
  `payout-senior-approver`.

Verb correctness: POST/submit paths take `create` (or the dedicated `submit`),
reads take `view`/`report.view`. Every view is `IsAuthenticatedAndActive &
HasRBACPermission`.

**Tenant isolation.** Every endpoint `resolve_entity(request)` +
`.filter(entity=entity)`; batch/payout detail lookups are `.filter(entity=entity,
pk=pk)` → a foreign pk 404s (`views.py:613,625,658`). Vendor and `source_account`
on create resolve **within the entity** (`views.py:403-409,499,513-516`), blocking
cross-tenant mass-assignment. Reconciliation only reads `BankStatementLine`s whose
`bank_account__entity == entity` (`reconciliation.py:191`).

**Approval SoD & scope.** Approvers resolve from the requesting tenant's active
role holders. An unstaffed stage parks the batch. The requester cannot approve
their own request, and one person cannot satisfy both high-value stages. Dispatch
independently recounts distinct approving actors from the immutable action log.

**FLS.** `PayoutInstructionSerializer.read_permissions` masks beneficiary name,
account number, and bank code unless the caller holds
`payments.payout.view_sensitive`; list pagination passes the request context, so
the same rule applies on list and detail responses. The movements feed also masks
its payout beneficiary fields.

## 10. Code map

- `models.py:176-421` - `PayoutBatch` (including idempotency fields and workflow bridge), `PayoutInstruction`,
  `PaymentEvent`.
- `constants.py:67-139` - payout / batch statuses + terminal sets, audit actions.
- `services.py:400-855` - retired `initiate_payout`, vendor snapshot checks,
  idempotent batch creation, exact approval validation, `_dispatch_transfer`,
  `create_payout_batch`, `submit_payout_batch`, `_recompute_batch_status`,
  `confirm_payout`, `_refresh_batch`, `_book_vendor_payment`.
- `workflow_handlers.py` - `PayoutBatchApprovalHandler` (the approval gate and
  explicit refusal of continue-without-approval).
- `reconciliation.py` - `settlement_reconciliation` + the row/summary dataclasses.
- `views.py:359-1033` - payout, batch (+ submit-for-approval), reconciliation,
  transactions, movements views; `_movement_querysets` (`views.py:907-937`).
- `serializers.py:67-151` - payout / batch / batch-summary / payment-event
  serializers (+ FLS).
- `vs_procurement/payables.py:352-563` - `post_vendor_payment` (the payout journal).
- `approvals.py` - `_default_stages_payload`, `ensure_default_approval_templates`
  (platform fallback, upserts), `ensure_tenant_approval_templates` (per tenant,
  non-destructive) - the seeded ladder itself.
- `provisioning.py` + `apps.py:20-27` - registering that seed against finance's
  entity provisioning, so the gate arrives with the books.
- `management/commands/seed_payout_approvals.py` - the one-time seed for entities
  provisioned before `681456f`.
- `vs_procurement.models.Vendor` - authoritative bank destination; changing a bank
  field resets verified KYC to PENDING.
- `vs_workflow/handlers/base.py`, `services/release.py`, and `views.py` - handler
  policy and typed refusal for unsafe continue-without-approval release.

## 11. Test coverage & gaps

Full `vs_payments` app suite: **160 green** (`python manage.py test vs_payments
--settings=apps.settings.local --noinput`, one app at a time with a unique
`DB_NAME`). Settlement-relevant:
- `PayoutTests`: retired standalone initiation refuses; confirm books
  `VendorPayment`; webhook confirmation re-verifies; failed payout books nothing.
- `PayoutBatchTests` (`tests.py:724-798`): assemble without submit; submit
  dispatches every item; confirming all → COMPLETED; partial failure →
  PARTIALLY_COMPLETED.
- `PayoutBatchApprovalTests` (45 tests): missing idempotency keys, exact replay and
  changed-payload conflict, 403 create refusal, cross-tenant vendor and batch
  isolation, missing-template rollback, verified vendor gates and legacy-field
  mismatch, post-approval bank change, direct-route refusal, requester separation,
  distinct senior approval, same-person refusal, payout release-policy refusal,
  wrong workflow instance, locked batch tampering, and provider dispatch only
  after the required terminal approval.
- `PayoutApprovalSeedingTests` (`tests.py:1971-2104`) and `PayoutOnboardingSeedTests`
  (`tests.py:2977-3051`): the seeded ladder's two-stage shape, the non-destructive re-seed, the command's arguments and
  `--dry-run`, and that creating an entity's books publishes the ladder with the
  approving role held by nobody.
- `SettlementReconciliationTests` (`tests.py:802-973`): reference match; net/fee
  carried from the bank line; amount-fallback for a payout; unsettled + unexplained
  break reconciliation; date-window filters both sides.
- `PaymentEventTests` (`tests.py:977-988`): append-only (save/delete raise).
- `PaymentsAPITests`: payout endpoint, create+submit batch, batch resolves vendor
  by code / requires one, settlement-reconciliation endpoint, transactions log,
  plus the hardening tests `test_payout_batch_summary_queued_counts_only_in_flight_children`
  and `test_movements_feed_hides_internal_linked_id`.
- Hardening additions: `WebhookTests.test_webhook_received_event_is_attributed_to_the_entity`,
  `PayoutTests.test_payout_adopts_provider_settled_amount` /
  `.test_confirm_payout_status_without_amount_keeps_instructed`,
  `SettlementReconciliationTests.test_over_settlement_fee_is_clamped_to_zero`,
  `SettlementReconciliationTests.test_amount_match_prefers_the_date_nearest_bank_line`.

Gaps still open:
- **Read/report permission denial** - payout creation has a 403 regression test;
  the report and submit-for-approval permission-denied cases remain uncovered.
- **Movements feed** - beyond the `linked_id` check, `/movements/` union +
  `direction` filter + FLS masking of payout PII, and `/movements/summary/`, remain
  lightly covered.
- **Movements-specific FLS negative case** - batch detail masking is covered,
  including bank code, but the movements response lacks the equivalent negative test.
