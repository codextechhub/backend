# payment_settlement — payouts, batches, reconciliation & the money feeds

> Slice 2 of `vs_payments`. Covers **money-out** and the **read-side money views**:
> the `PayoutInstruction` (single payout), the `PayoutBatch` (bulk disbursement,
> now approval-gated), the settlement-reconciliation report (gateway vs. bank),
> the unified movements feed, and the transactions log. Collections + virtual
> accounts are slice 1 (`payment_collections`); webhook ingestion + the PSP
> adapters are slice 3 (`payment_webhooks_providers`).

---

## 1. What it is (and what it is NOT)

A *payout* is a request to push money **out** of the entity to a beneficiary
through a PSP — the disbursement mirror of a collection. A `PayoutInstruction` is
the gateway record of that request; the authoritative money movement is a
**`vs_procurement.VendorPayment`** (Dr AP, Cr bank, Cr WHT), booked **only on
confirmation** (`services.py:510`, `_book_vendor_payment` at `services.py:536-559`).

A *payout batch* groups many instructions into one envelope an operator assembles
and submits once — the bulk-disbursement (payroll / vendor-run) unit. As of
`944ecee`, submitting a batch to the provider can be **approval-gated** via
`vs_workflow` (maker-checker), opt-in per published template.

The *settlement reconciliation*, *movements feed*, and *transactions log* are
**read-only** reports over the gateway records (and, for reconciliation, the
imported bank statement).

This does **NOT**:
- move money itself — the PSP does; we book the ledger mirror after confirmation.
- book anything at `initiate`/`submit` time — a `PENDING`/`PROCESSING` payout has
  no `vendor_payment_id` (`services.py:296-307`).
- **write** during reconciliation — it never mutates a bank line or books a
  journal; matching is advisory (`reconciliation.py:16`).
- gate money-out by default — approval only applies where a
  `payments.payout_batch` template is published for the batch's scope; **absent a
  template, a single `payments.payout.create` holder disburses directly** (§8).
- ignore a provider-reported settled amount — like collections, `confirm_payout`
  now adopts the PSP's settled figure when it differs (see §5/§8.3).

## 2. Domain model

### `PayoutInstruction` — `models.py:229-299`
One request to send money out. Money is integer **kobo** (`amount`).
- `entity` (PROTECT) — tenant scope; `batch` → `PayoutBatch` (nullable, the bulk
  envelope this belongs to).
- `reference` — our merchant reference / idempotency key, `unique`
  (`CXP-<uuid>`); `provider_reference` / `recipient_code` — the PSP's ids.
- `provider`, `amount`, `currency`.
- `beneficiary_name`, `beneficiary_account_number` (**both FLS-masked** — PII, §9),
  `beneficiary_bank_code`.
- `source_account` → `vs_finance.Account` (nullable — the bank/cash GL the booked
  payout credits; falls back to `1100`).
- `status` (`PayoutStatus`, default `PENDING`): `PENDING → PROCESSING → PAID |
  FAILED | REVERSED` (`constants.py:63-71`); terminal set `{PAID, FAILED,
  REVERSED}` (`constants.py:74-76`); `is_terminal` at `models.py:296-299`.
- **Loose ledger link (no hard FK into procurement):** `vendor_source_type` /
  `vendor_source_id` (the `Vendor` pk as a string) + `vendor_payment_id` (the
  booked `VendorPayment` pk), `models.py:273-276`.
- `failure_reason`, `metadata` (carries `wht_amount`), `raw_response`,
  `confirmed_at`, `created_by`.
- Indexes `(entity, status)`, `(provider, provider_reference)`.

### `PayoutBatch` — `models.py:171-247`
A bulk-disbursement envelope grouping many instructions.
- `entity` (PROTECT), `provider`, `reference` (`unique`), `title`, `narration`.
- `status` (`PayoutBatchStatus`, default `DRAFT`): `DRAFT → PROCESSING → COMPLETED
  | PARTIALLY_COMPLETED | FAILED` (`constants.py:79-100`); terminal set
  `{COMPLETED, PARTIALLY_COMPLETED, FAILED}`.
- `total_amount` / `item_count` — **denormalised** sums of the children, kept in
  sync by the service (`services.py:409-411`).
- `source_account` (default bank/cash GL for the children), `currency`,
  `submitted_at`, `metadata` (carries `approval_status`), `created_by`.
- **Workflow bridge (no migration):** `workflow_document_type =
  "payments.payout_batch"` (`models.py:187`); `school` property maps the
  entity-scoped batch to `entity.source_school` for the school-scoped approval
  engine, `branch` is always `None` (`models.py:233-247`).

### `PaymentEvent` — `models.py:351-391`
Append-only, immutable gateway action log (the transactions log). `save()` on an
existing pk and `delete()` both raise `ValueError` (`models.py:382-388`). `entity`
is **nullable** — and webhook-received/rejected events are written with no entity
(§8). Carries `action` (`PaymentAuditAction`), `provider`, `reference`,
`succeeded`, `message`, `metadata`, `actor_user`.

## 3. Endpoint map

Base `/v1/payments/`; all require `?entity=<id|code>`, platform envelope + RBAC.

| Method + path | permission key | what it does | request body (fields actually read) | response shape |
|---|---|---|---|---|
| `GET /payouts/` | `payments.payout.view` | list instructions, paginated | query: `group` (PENDING/PAID/FAILED), `status`, `provider` | `{pagination, data:[PayoutInstructionSerializer]}` |
| `POST /payouts/` | `payments.payout.create` | initiate a single payout (calls PSP now) | `amount`(kobo,>0), `beneficiary_name`**, `beneficiary_account_number`**, `beneficiary_bank_code`, `vendor`**, `source_account`, `provider`, `narration`, `wht_amount`, `metadata` | `success_response(data=PayoutInstructionSerializer, 201)` |
| `GET /payouts/summary/` | `payments.payout.view` | KPI totals (settled 7d, pending, failed) + group counts | query: `provider` | `success_response(data={total, settled7d, pending, failed, group_counts})` |
| `GET /payout-batches/` | `payments.payout.view` | list batches (summary serializer, no child array) | query: `status` | `{pagination, data:[PayoutBatchSummarySerializer]}` |
| `POST /payout-batches/` | `payments.payout.create` | assemble a DRAFT batch + children; `submit:true` dispatches **only if not approval-gated** | `items:[{amount, beneficiary_name, beneficiary_account_number, beneficiary_bank_code, vendor**, narration, wht_amount, metadata}]`, `source_account`, `provider`, `title`, `narration`, `submit` | `success_response(data=PayoutBatchSerializer, 201)` |
| `GET /payout-batches/summary/` | `payments.payout.view` | batch KPI totals (queued, completed7d, drafts) | — | `success_response(data={total, queued, completed7d, drafts})` |
| `GET /payout-batches/<pk>/` | `payments.payout.view` | one batch **with** its child instructions | — | `success_response(data=PayoutBatchSerializer)` |
| `POST /payout-batches/<pk>/` | `payments.payout.create` | **direct** submit pending children — **refused (400) if approval-gated** | — | `success_response(data=PayoutBatchSerializer)` |
| `POST /payout-batches/<pk>/submit-for-approval/` | `payments.payout_batch.submit` | route the batch through the vs_workflow approval engine | — | `success_response(data=PayoutBatchSerializer)` |
| `GET /reports/settlement-reconciliation/` | `payments.report.view` | gateway-confirmed movements vs. imported bank lines | query: `start_date`, `end_date` (ISO, inclusive), `provider` | `success_response(data={…, summary, rows[], unmatched_bank_lines[]})` |
| `GET /transactions/` | `payments.report.view` | the append-only gateway action log | query: `action`, `provider`, `succeeded` | `{pagination, data:[PaymentEventSerializer]}` |
| `GET /movements/` | `payments.report.view` | unified in+out feed, newest first; payout PII FLS-masked | query: `direction` (in/out), `group`, `provider` | `{pagination, data:[row]}` |
| `GET /movements/summary/` | `payments.report.view` | in7d / out7d / pending / failed across both gateways | query: `provider` | `success_response(data={in7d, out7d, pending, failed})` |

** = required. Notes:
- `amount` is `int(body.get("amount") or 0)` and must be `> 0`. `vendor` resolves
  **within the entity** by code or pk (`views.py:489-495`); a missing/foreign
  vendor is a 400. `source_account` resolves within-entity via `_entity_obj`.
- On `POST /payout-batches/` with `submit:true`, the direct dispatch is **skipped
  when a template exists** (`approval_required(batch)`, `views.py:514-517`) — the
  response message tells the caller to route it for approval instead.
- **Approval-gate approve/reject/return** are driven through the **vs_workflow**
  action endpoints (not vs_payments URLs); the handler's `on_approved` calls
  `submit_payout_batch`. Keys `payments.payout_batch.approve` /
  `.approve_high_value` gate those votes (§9).

## 4. Lifecycle / state machine

### Single payout
```
POST /payouts/          confirm (webhook re-verify OR ?status)
draft ──initiate──► PROCESSING ─────────────────────────────► PAID   (books vendor payment)
  │ _dispatch_transfer     │                                └► FAILED / REVERSED  (no ledger)
  └ provider rejects ─────►│ FAILED (no ledger; rejection audited)
```
`initiate_payout` creates the row `PENDING` then immediately `_dispatch_transfer`
→ PROCESSING on the PSP's accept, or FAILED on rejection (`services.py:279-352`).
Confirmation funnels through `confirm_payout` (`services.py:482-523`), idempotent
via `select_for_update` + terminal short-circuit. A webhook triggers
`confirm_payout(payout)` with **no** status, so it **re-verifies** against the PSP
(`webhooks.py:115`; see §8/slice 3) rather than trusting the event.

### Payout batch — ungated (no template)
`create_payout_batch` → **DRAFT** with PENDING children (`services.py:360-418`).
`submit_payout_batch` loops `_dispatch_transfer` over each PENDING child
(best-effort — a per-item rejection marks that child FAILED, does not abort),
then `_recompute_batch_status` derives the aggregate (`services.py:422-479`).
Confirming children moves the batch COMPLETED / PARTIALLY_COMPLETED / FAILED.

### Payout batch — approval-gated (template published)
```
create (DRAFT)                submit-for-approval            checker APPROVES
DRAFT ─────────► DRAFT + meta.approval_status=PENDING_APPROVAL ─────────► on_approved:
  (direct submit 400)          (no provider dispatch)            submit_payout_batch → PROCESSING
                                     │ REJECT/RETURN → meta.approval_status=DRAFT
```
The batch `status` stays `DRAFT` throughout approval; the phase lives in
`metadata["approval_status"]` (`workflow_handlers.py:62-68`). `validate_document`
preflights (must be a DRAFT batch with ≥1 PENDING child, `workflow_handlers.py:74-86`).
`on_approved` row-locks, marks `APPROVED`, and dispatches to the PSP as the **final
approver** (read from the workflow action log, `workflow_handlers.py:41-60,105-120`).

## 5. Calculations

**Payout net & journal split** (`_book_vendor_payment`, `services.py:536-559` →
`vs_procurement/payables.py:268-343`), all kobo:
- `gross = payout.amount` — where `confirm_payout` first adopts the PSP's settled
  amount when it reports one that differs (`settled = amount or payout.amount`,
  keeping the original in `metadata["instructed_amount"]`, `services.py:522-527`);
  `wht = metadata["wht_amount"]` (default 0);
  `net = gross − wht` (`payables.py:292`). Guard: `0 ≤ wht ≤ gross` else
  `PostingError` (`payables.py:290`). Example: instructed `70 000`, `wht = 7 000`
  → net `63 000`.

**Batch totals** — `total_amount = Σ child amounts`, `item_count = len(items)`,
computed once at assembly (`services.py:408-411`); not recomputed on child failure
(a FAILED child still counts toward `total_amount`).

**Reconciliation, signed kobo** (`reconciliation.py`):
- Gateway sign: collection `+amount`, payout `−amount` (`reconciliation.py:161,172`);
  bank line `amount` is already signed (+in/−out). A correct pairing nets to zero.
- `fee_amount = |gateway amount| − |settled bank amount|` — the PSP fee
  (`reconciliation.py:57-61`). Example: gross `40 000` settles to a `39 100` bank
  line → fee `900`.
- `is_reconciled` iff `unsettled_count == 0 and no unmatched bank lines`
  (`reconciliation.py:125-129`).

**Movements/summary** — 7-day money-in = `Σ collection.amount where status=SUCCEEDED
and confirmed_at ≥ now−7d`; money-out likewise for `status=PAID`
(`views.py:872-881`).

## 6. What posting does to the ledger

Only a **PAID** payout posts. `_book_vendor_payment` builds a draft
`vs_procurement.VendorPayment` and calls `post_vendor_payment`
(`payables.py:246-343`). Journal (source `BANK`), for gross `G`, WHT `W`,
net `N = G − W`:

| Dr / Cr | account | amount |
|---|---|---|
| **Dr** | vendor AP control (`vendor.payable_account`) | `G` |
| **Cr** | `source_account` (else fallback `1100` Cash & bank) | `N` |
| **Cr** | WHT payable (tax-code `collected_account`, else `WHT_PAYABLE_CODE`) | `W` (only if > 0) |

Carried vs dropped:
- `amount → gross_amount`, `metadata.wht_amount → wht_amount`, `currency`,
  `reference`, `narration`, `source_account → payment_account` all carry onto the
  `VendorPayment` (`services.py:547-557`).
- **Vendor is required to post.** `_book_vendor_payment` raises `PaymentStateError`
  if `vendor_source_id` is empty (`services.py:538-541`); it re-resolves the
  `Vendor` from the stored pk. A vendor `on_hold` blocks posting at the procurement
  layer (`payables.py:278`).
- **Auto-allocation to bills.** `post_vendor_payment` runs with its default
  `auto_allocate=True` (`_book_vendor_payment` passes no flag), so the gateway
  payout **settles the vendor's oldest open bills** (`payables.py:341-342`,
  `allocate_vendor_payment`). Parallel to the collections auto-allocation, but for
  a vendor payment this is the intended AP behaviour (§8).
- A `PayoutBatch` posts **nothing itself** — each *child* posts its own
  `VendorPayment` on confirmation.

## 7. Worked example

Batch of two, approval-gated (from `PayoutBatchApprovalTests`,
`tests.py:814-1017`):

1. Publish a `payments.payout_batch` template for the school (checker stage,
   `approver_permission_key = payments.payout_batch.approve`, `SCHOOL` scope,
   `tests.py:899-911`).
2. `create_payout_batch(items=[10000, 20000])` → batch **DRAFT**, two PENDING
   children, `total_amount=30000`, `item_count=2`.
3. `POST /payout-batches/<id>/` (direct submit) → **400** "approval-gated; submit
   it for approval instead" (`views.py:598-602`). Batch stays DRAFT.
4. `POST /payout-batches/<id>/submit-for-approval/` → a `WorkflowInstance` is
   created, stage 1 ACTIVE, `metadata.approval_status = PENDING_APPROVAL`; **no
   PSP dispatch** (`tests.py:967-978`).
5. The requester **cannot** approve their own batch (SoD enforced by the engine,
   `tests.py:980-997`). A distinct approver holding `payments.payout_batch.approve`
   votes APPROVE → `on_approved` dispatches → `submit_payout_batch` moves children
   to PROCESSING and the batch to PROCESSING.
6. Each child later confirmed PAID → books a `VendorPayment` (Dr `2100` AP / Cr
   `1100` bank) and the batch recomputes to COMPLETED.

## 8. Gotchas / known limitations

> Hardening pass (2026-07-12) closed items 2, 3, 5, 6, 7. Item 1 is tracked as an
> operational go-live task (`todo.md`); item 4 is accepted as a labelled heuristic.

1. ⚠️ **Maker-checker is opt-in per template — no template means single-actor
   disbursement.** `approval_required(batch)` is false without a published
   `payments.payout_batch` template, so a lone `payments.payout.create` holder can
   `POST /payout-batches/<id>/` (or create-with-`submit:true`) and push money out
   with no second approver (`views.py:514-517,598-603`). **By design** (mirrors the
   finance approval slices), but a real control gap until a template is published
   per scope. **Open — operational:** tracked in `todo.md` (seed a
   `payments.payout_batch` approval template for every live entity before go-live).

2. ✅ **Webhook events now carry the matched record's entity.** `ingest_webhook`
   resolves the target collection/payout once (`_find_record`) and passes its
   `entity` into the `WEBHOOK_RECEIVED` `PaymentEvent` (`webhooks.py:74-82`), so
   webhook actions appear in that entity's transactions log. The bad-signature
   `WEBHOOK_REJECTED` row intentionally stays `entity=NULL` (the payload is
   untrusted, so no entity can be attributed). Test:
   `test_webhook_received_event_is_attributed_to_the_entity`.

3. ✅ **Payouts now adopt the provider-reported settled amount.** `TransferResult`
   carries `amount` (`providers/base.py:74`, populated by Paystack/OPay/Fake);
   `confirm_payout` computes `settled = amount or payout.amount` and, when the PSP
   reports a positive figure that differs, stashes `metadata["instructed_amount"]`
   and books the settled gross (`services.py:507,522-527`). A `0` report never
   overrides. Tests: `test_payout_adopts_provider_settled_amount`,
   `test_confirm_payout_status_without_amount_keeps_instructed`.

4. ⚠️ **Reconciliation amount-fallback can mis-pair look-alike amounts.** Pass 2
   matches purely on exact signed amount within the window, first-unconsumed-wins
   (`reconciliation.py:217-229`) — the backend is **unchanged**. Two same-amount
   movements + two same-amount bank lines can pair the *wrong* way; harmless
   (read-only, advisory) but the row's `settlement_*` fields may point at the wrong
   bank line. **Accepted as a labelled heuristic** — the console flags amount
   matches (`match_basis == "amount"`) for a human to confirm.

5. ✅ **`fee_amount` is clamped at zero.** `max(0, |gross| − |settled|)`
   (`reconciliation.py:61`), so an over-settlement / reversal never displays a
   negative fee. Test: `test_over_settlement_fee_is_clamped_to_zero`.

6. ✅ **The "queued" KPI now counts only in-flight children.**
   `PayoutBatchSummaryView` sums child `PayoutInstruction` amounts where
   `batch is not null and status in (PENDING, PROCESSING)` instead of the batches'
   denormalised `total_amount` (`views.py:551-560`), so a FAILED child no longer
   inflates money-in-flight. (`total_amount` itself remains the assembly sum — the
   batch's face value.) Test:
   `test_payout_batch_summary_queued_counts_only_in_flight_children`.

7. ✅ **Movements feed no longer exposes internal ledger ids.** `linked_id` (the
   `payment_id` / `vendor_payment_id`) was dropped from the projection
   (`views.py:768-798`); `party` + `beneficiary_account` stay FLS-masked and
   `narration` is intentionally kept. Test: `test_movements_feed_hides_internal_linked_id`.

## 9. Permissions & tenant isolation

Keys (`seed_payments_permissions.py:26-34`), granted to `xvs_super_admin` /
`xvs_platform_admin`:
- `payments.payout.view` (NORMAL) — list/detail/summary/batches.
- `payments.payout.create` (**CRITICAL**) — POST payout, POST batch, direct batch
  submit.
- `payments.payout.view_sensitive` (SENSITIVE) — unmask beneficiary name/account
  (serializer FLS + movements masking).
- `payments.report.view` (NORMAL) — reconciliation, transactions, movements.
- `payments.payout_batch.submit` (SENSITIVE) — route a batch for approval.
- `payments.payout_batch.approve` / `.approve_high_value` (**CRITICAL**) — the
  approval votes (consumed by vs_workflow stages, not a vs_payments view).

Verb correctness: POST/submit paths take `create` (or the dedicated `submit`),
reads take `view`/`report.view`. Every view is `IsAuthenticatedAndActive &
HasRBACPermission`.

**Tenant isolation.** Every endpoint `resolve_entity(request)` +
`.filter(entity=entity)`; batch/payout detail lookups are `.filter(entity=entity,
pk=pk)` → a foreign pk 404s (`views.py:583,595,628`). Vendor and `source_account`
on create resolve **within the entity** (`views.py:489-495`), blocking
cross-tenant mass-assignment. Reconciliation only reads `BankStatementLine`s whose
`bank_account__entity == entity` (`reconciliation.py:175`).

**Approval SoD & scope.** The batch bridges to the school-scoped engine via
`batch.school = entity.source_school` (`models.py:233-241`); approvers are resolved
from that school's role holders of `payments.payout_batch.approve`. The requester
cannot approve their own batch (engine-enforced separation of duties,
`tests.py:980-997`).

**FLS.** `PayoutInstructionSerializer.read_permissions` masks `beneficiary_name` /
`beneficiary_account_number` unless the caller holds
`payments.payout.view_sensitive` (`serializers.py:66-92`); the movements feed masks
the same fields manually (`views.py:836-838`).

## 10. Code map

- `models.py:171-391` — `PayoutBatch` (+ workflow bridge), `PayoutInstruction`,
  `PaymentEvent`.
- `constants.py:63-131` — payout / batch statuses + terminal sets, audit actions.
- `services.py:279-559` — `initiate_payout`, `_dispatch_transfer`,
  `create_payout_batch`, `submit_payout_batch`, `_recompute_batch_status`,
  `confirm_payout`, `_refresh_batch`, `_book_vendor_payment`.
- `workflow_handlers.py` — `PayoutBatchApprovalHandler` (the approval gate).
- `reconciliation.py` — `settlement_reconciliation` + the row/summary dataclasses.
- `views.py:336-887` — payout, batch (+ submit-for-approval), reconciliation,
  transactions, movements views; `_movement_querysets` (`views.py:768-798`).
- `serializers.py:66-149` — payout / batch / batch-summary / payment-event
  serializers (+ FLS).
- `vs_procurement/payables.py:246-343` — `post_vendor_payment` (the payout journal).
- `vs_finance/approvals.py` — `approval_required` (the opt-in gate, shared with
  finance).

## 11. Test coverage & gaps

Baseline after hardening: **54 green** (`python manage.py test vs_payments
--settings=apps.settings.local`). Settlement-relevant:
- `PayoutTests` (`tests.py:308-354`): initiate→PROCESSING; confirm→books
  `VendorPayment` (gross carried); webhook confirm (re-verify); failed payout books
  nothing.
- `PayoutBatchTests` (`tests.py:380-449`): assemble without submit; submit
  dispatches every item; confirming all → COMPLETED; partial failure →
  PARTIALLY_COMPLETED.
- `PayoutBatchApprovalTests` (`tests.py:814-1017`): gate off → direct submit works;
  gate on → direct submit 400; submit-for-approval marks PENDING and does **not**
  dispatch; requester cannot approve own batch (SoD); approval dispatches; reject →
  DRAFT.
- `SettlementReconciliationTests` (`tests.py:452-549`): reference match; net/fee
  carried from the bank line; amount-fallback for a payout; unsettled + unexplained
  break reconciliation; date-window filters both sides.
- `PaymentEventTests` (`tests.py:553-564`): append-only (save/delete raise).
- `PaymentsAPITests`: payout endpoint, create+submit batch, batch resolves vendor
  by code / requires one, settlement-reconciliation endpoint, transactions log,
  plus the hardening tests `test_payout_batch_summary_queued_counts_only_in_flight_children`
  and `test_movements_feed_hides_internal_linked_id`.
- Hardening additions: `WebhookTests.test_webhook_received_event_is_attributed_to_the_entity`,
  `PayoutTests.test_payout_adopts_provider_settled_amount` /
  `.test_confirm_payout_status_without_amount_keeps_instructed`,
  `SettlementReconciliationTests.test_over_settlement_fee_is_clamped_to_zero`.

Gaps still open:
- **403 / permission-denied** — no test that a caller lacking `payout.create` /
  `report.view` / `payout_batch.submit` gets 403.
- **Cross-tenant isolation** — no test that a foreign batch/payout `pk` or
  `?entity` 404s on these routes.
- **Movements feed** — beyond the `linked_id` check, `/movements/` union +
  `direction` filter + FLS masking of payout PII, and `/movements/summary/`, remain
  lightly covered.
- **FLS negative case** — no test that a caller without `payout.view_sensitive`
  sees beneficiary details masked.
</content>
