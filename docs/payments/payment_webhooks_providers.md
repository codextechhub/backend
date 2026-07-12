# payment_webhooks_providers — webhook ingestion & PSP adapters

> Slice 3 (final) of `vs_payments`. Covers the **inbound edge**: the public webhook
> receiver, the ingest pipeline (verify → dedupe → store → dispatch → re-verify),
> and the provider adapters (Paystack, OPay, Fake) behind the neutral `Provider`
> interface — plus the registry, the HTTP transport, and the typed exceptions.
> Collections/VAs are slice 1; payouts/batches/reconciliation are slice 2. This
> slice is where an external PSP event first touches the system.

---

## 1. What it is (and what it is NOT)

A PSP tells us out-of-band that a charge settled or a transfer paid by POSTing a
signed **webhook** to `/v1/payments/webhooks/<provider>/`. This slice is the code
that receives it safely and the per-provider adapters that translate each PSP's
wire format into the app's neutral vocabulary.

Two hard rules (`webhooks.py:1-15`):
1. **Authenticity** — the raw body's signature must verify against the provider
   secret, else 401 and no action.
2. **Idempotency** — every event is stored under a unique `dedupe_key`; a retry of
   an already-processed event does nothing.

A third, security-critical rule was added since slice 1: **the webhook's claimed
status/amount is never trusted to move money.** A verified event only tells us
*which* transaction changed; the `confirm_*` services then **re-verify** the
authoritative status/amount against the PSP's API before booking anything
(`webhooks.py:92-101`; §4).

This does **NOT**:
- carry a JWT — the webhook endpoint is `AllowAny`; the **signature** is the auth
  (`views.py:895-916`).
- trust the event body's status/amount — it re-verifies (§4/§8).
- book the ledger here — it delegates to `confirm_collection` / `confirm_payout`
  (slices 1/2), and does so **off the request path** on a Celery worker (§4/§8.1).
- hold per-entity PSP credentials — one platform-level secret per provider (§8).
- make live network calls in tests — all HTTP funnels through one patchable
  function (`providers/http.py`).

## 2. Domain model

The only persisted model in this slice is **`WebhookEvent`** (`models.py:302-348`)
— the idempotency backbone + raw audit/replay store:
- `provider`, `event_type`, `provider_reference`.
- `dedupe_key` (**unique**) — the provider's event id, else `"<PROVIDER>:<sha256(body)>"`
  (`webhooks.py:57`).
- `signature`, `verified` (bool), `status` (`WebhookStatus`: RECEIVED / PROCESSED /
  IGNORED / FAILED, `constants.py:108-114`).
- `headers`, `payload` (parsed JSON), `raw_body` (verbatim text) — persisted
  **before** any processing, so an event is always replayable.
- `error`, `processed_at`, and nullable `collection` / `payout` FKs linking the
  event to the record it settled.

`WebhookEvent` is **not entity-scoped** (a webhook arrives before we know the
tenant); the entity is derived from the matched collection/payout at dispatch.
The `PaymentEvent` audit rows this slice writes are covered in slice 2 §2.

## 3. Endpoint map

| Method + path | auth | what it does | request body | response |
|---|---|---|---|---|
| `POST /webhooks/<provider>/` | **public** (`AllowAny`, no JWT) | verify → store → dispatch one PSP event | raw signed provider body (bytes) + provider headers | `success_response`; `{id, status}` on process, `{duplicate: true}` on a repeat |

Notes (`views.py:895-916`):
- `<provider>` is the URL segment (`paystack` / `opay` / `fake`), upper-cased and
  resolved via the registry; an unknown/unconfigured provider raises
  `ProviderNotConfiguredError` (503).
- `authentication_classes = []`, `permission_classes = [AllowAny]` — deliberately
  unauthenticated; authenticity is the body signature.
- `request.body` (raw bytes) + `dict(request.headers)` are handed to
  `ingest_webhook`. A `DuplicateWebhookError` is caught and turned into a **200**
  acknowledgement so the PSP stops retrying (`views.py:911-915`,
  `exceptions.py:49-59`).
- A `WebhookSignatureError` renders as **401**; any processing exception marks the
  event FAILED and re-raises (surfaced by the shared handler).

## 4. Lifecycle / the ingest pipeline

Processing is split into a **fast synchronous receiver** (`ingest_webhook`) and an
**async worker step** (`process_stored_event`, run by the Celery task
`vs_payments.process_webhook_event`):

```
POST /webhooks/<provider>/          ── ingest_webhook (fast, in-request) ──
      │
      ▼  client = get_provider(provider)                       registry.py:65-70
1. verify_signature(raw_body, headers) ── false ─► audit WEBHOOK_REJECTED (entity=None) ─► 401
      │ true
      ▼
2. payload = json.loads(raw_body)  ;  parsed = client.parse_webhook(...)
      ▼
3. dedupe_key = parsed.dedupe_key or "<PROVIDER>:<sha256(body)>"
   event, created = WebhookEvent.get_or_create(dedupe_key, defaults=…RECEIVED…)   ← idempotency
      │ not created AND status==PROCESSED ─► DuplicateWebhookError ─► 200
      ▼
4. if created:  record = _find_record(parsed);  audit WEBHOOK_RECEIVED (entity=record.entity)   ← audit-once
      ▼
5. transaction.on_commit(→ process_webhook_event.delay(event.id))   ;  return RECEIVED  ─► 200 ack

── process_stored_event(event_id)  (Celery worker; inline under CELERY_TASK_ALWAYS_EAGER) ──
   event gone / already PROCESSED ─► no-op (idempotent)
   re-derive parsed from stored payload/raw_body/headers ; record = _find_record(parsed)
   _dispatch(event, parsed, record):
      COLLECTION → confirm_collection(intent)   ← NO status ⇒ RE-VERIFY vs PSP     webhooks.py:104-111
      PAYOUT     → confirm_payout(payout)        ← NO status ⇒ RE-VERIFY vs PSP     webhooks.py:112-120
      no match / unknown dir → status=IGNORED
      → status=PROCESSED, processed_at set
      │ any exception → status=FAILED, error saved, swallowed (PSP re-delivers; confirm_* idempotent)
```

The receiver stores-and-acks fast; the outbound PSP re-verify + booking happen off
the request path on a worker (§8.1). Under `CELERY_TASK_ALWAYS_EAGER` (local/CI, and
staging until the worker is live) the task runs inline, so behaviour is unchanged
there; with a real worker the webhook returns `RECEIVED` immediately and the worker
flips it to `PROCESSED`/`IGNORED`/`FAILED`.

The **re-verify** step is the security spine: `_dispatch` calls
`confirm_collection(intent)` / `confirm_payout(payout)` with **no** status, so those
services poll `verify_collection` / `verify_transfer` and act on the PSP's own
answer — a forged-but-signed `charge.success` (Paystack sets that regardless of the
inner txn state) can't book money unless the PSP's API also confirms it. Pinned by
`test_webhook_does_not_book_when_provider_verify_disagrees`.

**Matching** (`_find_collection` / `_find_payout`, `webhooks.py:135-160`): by our
`reference` first, then `provider_reference`; unscoped across entities (safe —
`reference` is globally unique; the entity is taken from the matched record).

## 5. Provider interface & the three adapters

The neutral contract is `Provider = CollectionProvider + PayoutProvider`
(`providers/base.py:154-156`), speaking **kobo** and our own status strings, with
the raw PSP payload preserved on `.raw`. Result dataclasses: `CheckoutResult`,
`VirtualAccountResult`, `CollectionStatusResult`, `TransferResult` (now carries
`amount`, slice 2 §8.3), `WebhookParseResult`.

| Capability | method | Paystack | OPay | Fake |
|---|---|---|---|---|
| create checkout | `create_checkout` | `POST /transaction/initialize` | `POST <create_path>` (signed) | deterministic URL |
| provision VA | `create_virtual_account` | `POST /customer` then `/dedicated_account` | **raises** `ProviderError` (not wired) | deterministic NUBAN |
| verify collection | `verify_collection` | `GET /transaction/verify/<ref>` | `POST <status_path>` (public key) | forced status/amount |
| create transfer | `create_transfer` | `/transferrecipient` then `/transfer` | `POST <transfer_path>` (signed) | fixed PROCESSING |
| verify transfer | `verify_transfer` | `GET /transfer/verify/<ref>` | `POST <transfer_status_path>` | forced status/amount |
| verify signature | `verify_signature` | HMAC-SHA512(body, secret) vs `x-paystack-signature` | HMAC-SHA512(sorted inner JSON, secret) vs body `sha512`/`Authorization` | HMAC-SHA512(body, secret) vs `x-fake-signature` |
| parse webhook | `parse_webhook` | `event` prefix `transfer` ⇒ PAYOUT | heuristic: `transferStatus`/`type~transfer` ⇒ PAYOUT | `event` prefix `transfer` ⇒ PAYOUT |

Status translation is per-adapter (`_COLLECTION_STATUS` / `_TRANSFER_STATUS` maps,
`paystack.py:28-42`, `opay.py:36-52`). Signature verification is constant-time
(`hmac.compare_digest`) on both real adapters.

**Auth models differ:** Paystack uses `Authorization: Bearer <secret_key>` on every
call (`paystack.py:55-56`). OPay signs write requests
(`Authorization: Bearer HMAC(payload, secret)` + `MerchantId`) but uses the
**public key** as bearer for status queries (`opay.py:80-93`); its hosts/paths are
injected from settings and an unset path raises rather than guessing
(`opay.py:81-82`).

**Registry** (`providers/registry.py`): `get_provider(name)` returns a
settings-built client, unless a test `register()`d an override (the suite points
`PAYSTACK` at a `FakeProvider`, so no live keys/network are ever used).

**Transport** (`providers/http.py`): one stdlib `request_json` — a non-2xx, a
transport failure, or a non-JSON body all become a typed `ProviderError` (→ 502).
Tests patch this single function.

## 6. What posting does to the ledger

**Nothing here posts directly.** This slice's job ends at calling
`confirm_collection` / `confirm_payout`; those book the receipt / vendor payment
(slice 1 §6, slice 2 §6). What this slice *guarantees* for posting is the two
invariants above: exactly-once (dedupe) and authenticity+truth (signature +
re-verify), so a retry, a race, or a forged event can't produce a second or a
bogus journal.

## 7. Worked example

Paystack collection webhook (from `test_webhook_confirms_collection`,
`tests.py:258-274`, via the `FakeProvider` wired over `PAYSTACK`):

1. `initiate_collection(...)` creates a PROCESSING intent with `reference=CXP-…`.
2. The provider (test double) reports success: `fake.forced_status[ref] = "SUCCEEDED"`.
3. `build_webhook(event="charge.success", reference=ref, status="SUCCEEDED",
   amount=40000)` returns `(raw_body, {x-fake-signature: HMAC})`.
4. `POST /webhooks/paystack/` → `ingest_webhook`: signature verifies →
   `parse_webhook` → COLLECTION, `dedupe_key="FAKE:charge.success:<ref>"` →
   `WebhookEvent` stored RECEIVED → `WEBHOOK_RECEIVED` audit (entity = intent's) →
   `_dispatch` → `confirm_collection(intent)` **re-verifies** (fake verify returns
   SUCCEEDED) → books the receipt → event **PROCESSED**.
5. A byte-identical retry: `get_or_create` finds the PROCESSED row →
   `DuplicateWebhookError` → **200**, no second receipt
   (`test_duplicate_webhook_never_double_books`, `tests.py:277-288`).

## 8. Gotchas / known limitations

> Hardening pass (2026-07-12) closed items 1 (async) and 2 (audit-once).

1. ✅ **Re-verify now runs off the request path.** `ingest_webhook` stores the event
   and `transaction.on_commit(→ process_webhook_event.delay(id))`, returning a
   `RECEIVED` 200 ack immediately; the outbound `verify_collection`/`verify_transfer`
   + booking run in `process_stored_event` on a Celery worker (`webhooks.py:90-129`,
   `tasks.py`). `process_stored_event` is idempotent (no-op if the event is gone or
   already `PROCESSED`) and swallows a dispatch failure after marking it `FAILED`
   (the PSP re-delivers; `confirm_*` are idempotent). Under
   `CELERY_TASK_ALWAYS_EAGER` (local/CI/pre-worker staging) it still runs inline.

2. ✅ **`WEBHOOK_RECEIVED` is now audited once.** The audit row is written only on
   `created` (`webhooks.py:82-89`), so a provider retry of a not-yet-processed event
   no longer adds a second `PaymentEvent`. The self-heal remains: a `DuplicateWebhookError`
   short-circuit fires only for a `PROCESSED` event, so an IGNORED/FAILED event is
   still reprocessed (and can now match) on re-delivery — just without the duplicate
   audit line. Test: `test_ingest_is_idempotent_and_audits_once`.

3. **One platform-level PSP secret per provider — no per-entity credentials.**
   `get_provider(provider)` builds the client from global settings
   (`registry.py:33-62`), so every entity's webhooks verify against the same
   Paystack/OPay account. **By design** (single merchant account per PSP); revisit
   only if the platform ever onboards per-tenant PSP sub-accounts.

4. **OPay virtual-account provisioning is unsupported.**
   `OPayProvider.create_virtual_account` raises `ProviderError`
   (`opay.py:127-134`), so `POST /virtual-accounts/ {provider: OPAY}` returns 502.
   **By design/config** — checkout is the OPay collection path; wire the dedicated
   OPay VA endpoint before offering OPay NUBANs.

5. **OPay webhook direction/field mapping is heuristic and defensive.** Direction is
   inferred from `type~"transfer"` or a `transferStatus` key
   (`opay.py:204-214`), and amounts read a nested `{"total": kobo}` shape; the
   adapter's own header NOTE warns field names vary by OPay product
   (`opay.py:14-16`). An unusual event shape could misroute or read amount 0
   (0 never overrides on confirm — slice 2 §8.3). **Known** — validate against
   onboarding docs before OPay go-live.

6. **Paystack VA creation mints a customer with a placeholder email** when none is
   supplied (`{reference}@example.com`, `paystack.py:102`). **Low severity** —
   data-quality only; pass a real `billing_email` upstream (the collections view
   already defaults it from the customer).

7. **Unmatched (IGNORED) events are not retried by us.** If a webhook arrives with a
   `reference`/`provider_reference` that matches no local record, it is stored
   IGNORED and only reprocessed if the **PSP** re-delivers it (item 2). There's no
   internal sweeper to re-match stored IGNORED events once the intent appears.
   **Low severity** given PSP retry behavior; note for an ops runbook.

## 9. Permissions & tenant isolation

- **The receiver has no RBAC** — it is `AllowAny` by necessity (a PSP can't carry a
  JWT). Authenticity is the **signature**; a bad/absent signature is a 401 and is
  audited as `WEBHOOK_REJECTED` (`webhooks.py:45-50`). Signature checks are
  constant-time on both real adapters.
- **No entity in the URL/body is trusted.** The tenant is derived from the matched
  collection/payout, whose own `entity` scoping governs the downstream booking; a
  webhook cannot direct money into an arbitrary entity — it can only advance the
  specific record its `reference` maps to.
- **Replay/duplication** can't double-book: unique `dedupe_key` + PROCESSED
  short-circuit + the `confirm_*` terminal-state guard (three independent layers).
- **Forged-but-signed status** can't book: the re-verify against the PSP API is the
  source of truth, not the event payload (§4).
- The bad-signature `WEBHOOK_REJECTED` audit intentionally carries `entity=None`
  (the payload is untrusted, so no entity can be attributed — slice 2 §8.2).

## 10. Code map

- `webhooks.py` — `ingest_webhook` (verify/dedupe/store/enqueue), `_enqueue`,
  `process_stored_event` (the worker step), `_dispatch`, `_find_record`/
  `_find_collection`/`_find_payout`.
- `tasks.py` — `process_webhook_event` Celery task (auto-discovered; enqueued via
  `on_commit`).
- `views.py:895-916` — `WebhookView` (public receiver).
- `providers/base.py` — neutral interface + result dataclasses.
- `providers/registry.py` — `get_provider` / `register` / `unregister`.
- `providers/http.py` — `request_json` (the single patchable network surface).
- `providers/paystack.py`, `providers/opay.py`, `providers/fake.py` — the adapters.
- `exceptions.py` — `ProviderError` (502), `ProviderNotConfiguredError` (503),
  `WebhookSignatureError` (401), `DuplicateWebhookError` (200), `PaymentStateError`
  (409).
- `constants.py:108-131` — `WebhookStatus`, `PaymentAuditAction`.

## 11. Test coverage & gaps

Baseline after hardening: **70 green** (`python manage.py test vs_payments
--settings=apps.settings.local`). Webhook/provider-relevant:
- `ProviderTests`: Fake signature round-trip (tamper → invalid); registry override
  resolves the Fake over `PAYSTACK`.
- `WebhookTests`: **bad signature rejected + books nothing**; webhook confirms a
  collection (re-verify path, async via `captureOnCommitCallbacks`); **duplicate
  never double-books**; **validly-signed but provider-verify-disagrees books
  nothing** (forged-success guard); `WEBHOOK_RECEIVED` attributed to the entity;
  **`test_ingest_is_idempotent_and_audits_once`** (audit-once + one receipt across
  two deliveries).
- **`PaystackAdapterTests`** (6) and **`OPayAdapterTests`** (7): drive the real
  adapters with recorded PSP payloads by patching `request_json` *at the point of
  use* (`providers.paystack.request_json` / `providers.opay.request_json`, since the
  adapters bind it via `from .http import`) — checkout, verify collection/transfer,
  non-ok → `ProviderError`, OPay VA unsupported, signature verify (pos/neg),
  `parse_webhook` direction routing.
- `WebhookProviderResolutionTests` (1): unknown provider → `ProviderNotConfiguredError`.
- `PaymentsAPITests.test_webhook_endpoint_processes_and_dedupes` — the public
  endpoint processes then dedupes to a 200.

Gaps still open:
- **Transport error paths** (`http.request_json`: non-2xx, non-JSON, URLError →
  `ProviderError`/502) are not directly tested.
- **The IGNORED self-heal-on-retry path** (§8.7 — an event that matched nothing on
  first delivery, then succeeds once the intent exists) is not pinned by a test.
</content>
