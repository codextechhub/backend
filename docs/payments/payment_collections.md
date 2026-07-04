# payment_collections — collections & virtual accounts

> Slice 1 of `vs_payments`. Covers **money-in**: the `CollectionIntent` gateway
> record, the `VirtualAccount` (dedicated NUBAN) record, and the endpoints/services
> that initiate, confirm and provision them. Payouts, batches, settlement
> reconciliation and the webhook receiver are separate slices
> (`payment_settlement`, `payment_webhooks_providers`).

---

## 1. What it is (and what it is NOT)

The payments app is the **gateway layer that sits in front of the ledger**
(`models.py:1-13`). A *collection* is a request to pull money **in** from a payer
through an external PSP (Paystack / OPay / a Fake test provider). A *virtual
account* is a dedicated NUBAN the PSP issues so a customer can pay by bank transfer
with no checkout step, and the deposit self-attributes to them.

Nothing in this slice is itself an accounting entry. A `CollectionIntent` only
records *what we asked the provider to do and what it told us*. The authoritative
money movement is a **`vs_finance.Payment` receipt** (Dr bank, Cr AR), and it is
booked **only when the collection is confirmed** — never at initiation
(`services.py:59-64`, `services.py:172-216`).

This does **NOT**:
- move money by itself — the provider does; we book the ledger mirror after the
  fact.
- book anything at `initiate` time — a `PENDING`/`PROCESSING` intent has no
  `payment` (`services.py:78-120`).
- reconcile against the bank statement — that is `SettlementReconciliation`
  (settlement slice).
- tear down a virtual account at the provider when you deactivate it locally
  (`services.py:150-169`).

## 2. Domain model

### `CollectionIntent` — `models.py:88-168`
One request to collect money in. Money is integer **kobo** (`amount`, a
`vs_finance.MoneyField`, `models.py:109`).

Key fields:
- `entity` → `vs_finance.LedgerEntity` (PROTECT) — the tenant scope (`models.py:97-99`).
- `reference` — **our** merchant reference / idempotency key, `unique` globally
  (a `CXP-<uuid>` string, `services.py:42-44`); `provider_reference` is what the
  PSP returns (`models.py:104-108`).
- `provider` (`PaymentProvider`: OPAY / PAYSTACK / FAKE), `channel`
  (`CollectionChannel`: CHECKOUT / VIRTUAL_ACCOUNT / CARD / BANK_TRANSFER / USSD),
  `constants.py:15-37`.
- `status` (`CollectionStatus`, default `PENDING`) — `PENDING → PROCESSING →
  SUCCEEDED | FAILED | ABANDONED | REFUNDED` (`constants.py:40-53`). Terminal set:
  `{SUCCEEDED, FAILED, ABANDONED, REFUNDED}` (`constants.py:57-60`); `is_terminal`
  at `models.py:164-168`.
- `customer` (nullable), `invoice` (nullable — the invoice this collection
  settles), `deposit_account` (nullable — the bank/cash GL the receipt debits),
  `virtual_account` (nullable — the VA it arrived through).
- `payment` → `vs_finance.Payment` (nullable) — the booked receipt, set on confirm
  (`models.py:140-144`).
- `checkout_url`, `authorization_code`, `payer_email`, `payer_name`, `narration`.
- `metadata` / `raw_response` — free `JSONField`s (payer-supplied + raw PSP body).
- `confirmed_at`, `created_by`.

Indexes: `(entity, status)`, `(provider, provider_reference)`, `(customer)`
(`models.py:153-158`). Ordering `-id`.

### `VirtualAccount` — `models.py:33-85`
A dedicated NUBAN issued by a provider for self-reconciling collection.
- `entity` (PROTECT), `provider`, `customer` (nullable), `deposit_account`
  (nullable — GL account collections into this NUBAN land in), `currency`.
- `account_number`, `bank_name`, `account_name` — the funding coordinates
  (`account_number`/`account_name` are **FLS-masked**, see §9).
- `provider_reference`, `status` (`VirtualAccountStatus`: ACTIVE / INACTIVE,
  default ACTIVE, `constants.py:103-105`), `raw` (`JSONField`).
- **Uniqueness:** only `uniq_payments_va_provider_account` on
  `(provider, account_number)` (`models.py:72-77`). The docstring's claim of "one
  active account per provider *per customer*" is **not** enforced by a constraint
  or a service check — see §8.
- Indexes `(entity, provider)`, `(customer)`.

Both are `TimeStampedModel` (reuses `vs_finance`) and scoped per `LedgerEntity`;
every read/write goes through the entity resolver (§9).

## 3. Endpoint map

Base: `/v1/payments/` (`urls.py`). All routes below require `?entity=<id|code>`
and use the platform envelope + RBAC, except where noted. Request body lists
**only fields the view actually reads**.

| Method + path | permission key | what it does | request body (fields actually read) | response shape |
|---|---|---|---|---|
| `GET /collections/` | `payments.collection.view` | list intents, newest first, paginated (XVSPagination, page 25) | query only: `group` (PENDING/PAID/FAILED/REFUNDED), `status`, `provider`, `virtual_account` | `{pagination, data:[CollectionIntentSerializer]}` |
| `POST /collections/` | `payments.collection.create` | initiate a collection (calls provider, stores checkout url) | `amount`(kobo, >0), `customer`, `invoice`, `deposit_account`, `channel`, `provider`, `payer_email`, `payer_name`, `narration`, `metadata` | `success_response(data=CollectionIntentSerializer, 201)` |
| `GET /collections/summary/` | `payments.collection.view` | KPI totals + status-group counts over ALL rows | query: `provider` | `success_response(data={total, collected, pending, failed, success_rate, group_counts})` |
| `GET /collections/<pk>/` | `payments.collection.view` | fetch one; `?verify=1` polls provider & confirms if settled | query: `verify` | `success_response(data=CollectionIntentSerializer)` |
| `GET /virtual-accounts/` | `payments.virtual_account.view` | list VAs, **custom** pagination + KPIs (see note) | query: `status`, `provider`, `customer`, `search`, `page`, `page_size` | `{success, message, pagination, kpis, data:[VirtualAccountSerializer]}` |
| `POST /virtual-accounts/` | `payments.virtual_account.create` | provision a dedicated NUBAN | `customer`(**required**), `deposit_account`, `provider`, `bank_code` | `success_response(data=VirtualAccountSerializer, 201)` |
| `GET /virtual-accounts/<pk>/` | `payments.virtual_account.view` | fetch one VA | — | `success_response(data=VirtualAccountSerializer)` |
| `PATCH /virtual-accounts/<pk>/` | `payments.virtual_account.manage` | activate / deactivate (local only) | `status` (ACTIVE/INACTIVE) | `success_response(data=VirtualAccountSerializer)` |

Notes:
- **`amount`, `customer`, `invoice`, `deposit_account` are the only body fields
  that matter on POST /collections/.** `amount` is coerced with
  `int(body.get("amount") or 0)` and must be `> 0` (`views.py:123-125`). `customer`
  / `invoice` / `deposit_account` resolve **within the entity** by pk **or code**
  via `_entity_obj` (`views.py:58-75`) — a ref from another tenant raises a 400.
- **VA list now uses the shared `_paginate` envelope** (`views.py:239-242`),
  routing through `XVSPagination` (page size **25**, real `next`/`previous`) and
  injecting the extra top-level `kpis` object onto `resp.data` — consistent with
  every other list in this app.
- There is no `?entity` exception here — every collections/VA route is
  entity-scoped (unlike `vs_finance` currencies/fx-rates).

## 4. Lifecycle / state machine

### Collection
```
        POST /collections/                 confirm (webhook OR ?verify=1)
draft ────────────────────► PROCESSING ───────────────────────────────► SUCCEEDED  (books receipt)
  │  initiate_collection         │                                    └► FAILED / ABANDONED  (no ledger)
  │                              │
  └─ provider rejects at init ──►│ FAILED  (no ledger; rejection audited)
```
- `initiate_collection` creates the row `PENDING`, calls
  `client.create_checkout(...)`; on success it flips to **PROCESSING** with the
  checkout url + provider ref (`services.py:88-120`). On provider rejection it flips
  to **FAILED**, stores the error, writes a durable rejection audit row, and
  re-raises (`services.py:95-103`).
- Confirmation is driven **two ways**, both funnelling through
  `confirm_collection` (`services.py:172-216`):
  1. `GET /collections/<pk>/?verify=1` → `confirm_collection(intent)` with no
     status → polls `client.verify_collection(...)` (`views.py:199-201`).
  2. an inbound webhook → `confirm_collection(intent, status=parsed.status)`
     (webhook slice).
- `SUCCEEDED` books the receipt and is terminal; `FAILED`/`ABANDONED` are terminal
  with no ledger effect. `REFUNDED` exists in the enum but **no endpoint or service
  transitions into it** in this slice (§8).

### Virtual account
`create_virtual_account` → **ACTIVE** (`services.py:135-141`).
`PATCH …/status/` → `set_virtual_account_status` flips ACTIVE ⇄ INACTIVE locally
and audits it; same-status is a no-op; unknown status → 400
(`services.py:150-169`). Deactivation is **local-only** (no provider teardown).

## 5. Calculations

This slice has almost no arithmetic of its own — the money value is carried
verbatim (`amount`, kobo) from request → intent → receipt. The two computed
surfaces:

**Booked receipt amount.** `_book_receipt` books `Payment.amount = intent.amount`
(`services.py:231-237`). As of the hardening pass, `confirm_collection` first
adopts the **settled** amount: `settled = amount or intent.amount`; if
`settled > 0 and settled != intent.amount` it stashes the original in
`intent.metadata["requested_amount"]` and overwrites `intent.amount = settled`
before booking (`services.py:214-220`). So `booked = settled` when the provider
reports one, else the requested amount. Example: intent for `5 000 000` kobo
(₦50,000); webhook reports `amount: 4 900 000` → we book **4 900 000** and keep
`requested_amount: 5 000 000` on metadata. (A `0` report never overrides — the
FakeProvider `verify` reports 0, so verify-driven confirms keep the requested
amount.)

**Collections summary success rate** (`views.py:170-171`):
`rate = round(paid_count × 100 / (paid_count + failed_count))`, `None` when no
terminal rows. Group sums (`collected`/`pending`/`failed`) are `Sum(amount, filter=…)`
coalesced to 0 over the whole entity (`views.py:160-169`) — kobo, no rounding.

**VA KPIs** (`views.py:225-230`): plain `count()`s — total / active / inactive /
distinct providers.

## 6. What posting does to the ledger

Only a **SUCCEEDED** collection posts. `_book_receipt` (`services.py:219-241`)
builds a draft `vs_finance.Payment` and calls
`vs_finance.receivables.post_payment` (`receivables.py:226-245, 330-398`).

Journal (source `BANK`), for a receipt of `A` kobo with `applied` allocated to
invoices and `excess = A − applied`:

| Dr / Cr | account | amount |
|---|---|---|
| **Dr** | `deposit_account` (the intent's, else fallback `1100` Cash & bank) | `A` |
| **Cr** | customer's AR control (`customer.receivable_account`) | `applied` |
| **Cr** | customer credit `2140` (liability) | `excess` (only if > 0) |

Carried vs dropped on the way to the ledger:
- **`amount`, `customer`, `currency`, `reference`, `narration`, `deposit_account`**
  carry onto the `Payment` (`services.py:231-237`).
- **`deposit_account` fallback:** if the intent has none, the receipt debits
  `resolve_account(entity, CASH_BANK_CODE="1100")` (`services.py:228-230`,
  `vs_finance/constants.py:564`).
- **Allocation:** if the intent has an `invoice`, `allocations = [(invoice, amount)]`
  — a fixed split against that invoice. **If it has no invoice, `_book_receipt`
  now passes `auto_allocate=False`** (`services.py:259-264`), so a standalone
  collection is parked in `2140` customer credit rather than silently settling the
  customer's open invoices. (Standalone receipts are a confirmed use case.)
- **Requires a customer at initiation.** `initiate_collection` now raises
  `ValidationError` (→ 400) if no customer is given/derivable **before** creating
  the intent or calling the provider (`services.py:77-78`), so a customer-less
  collection can no longer be started. `_book_receipt` keeps its defensive
  `customer_id is None` guard (`services.py:245-247`).
- **Inactive-VA hold.** If the intent is linked to an INACTIVE virtual account,
  `_book_receipt` raises `PaymentStateError` before booking (`services.py:241-244`),
  so the deposit is held (webhook marked FAILED, retained for replay) instead of
  auto-posting to a deactivated NUBAN.
- The intent's `payment` FK is set, `status=SUCCEEDED`, `confirmed_at=now`
  (`services.py:206-209`), and a `COLLECTION_CONFIRMED` `PaymentEvent` is written
  in the same transaction (`services.py:210-215`).

`VirtualAccount` provisioning posts **nothing** — it only stores the NUBAN.

## 7. Worked example

Using the `FakeProvider` (test wiring, `tests.py:119-151`,
`providers/fake.py:39-69`):

1. Seed a customer `CUST1` with AR `1200`; post an invoice for `50 000` kobo.
2. `POST /collections/ {amount: 50000, customer: CUST1, invoice: <id>}` →
   `initiate_collection`. Fake `create_checkout` returns
   `provider_reference="FAKE-<ref>"`, `checkout_url="https://fake.test/checkout/<ref>"`,
   status PENDING. Intent saved **PROCESSING**. Response (201):
   ```json
   { "id": 1, "provider": "PAYSTACK", "channel": "CHECKOUT",
     "reference": "CXP-…", "provider_reference": "FAKE-CXP-…",
     "amount": 50000, "amount_naira": "₦500.00", "status": "PROCESSING",
     "customer_code": "CUST1", "invoice_id": 7, "checkout_url": "https://fake.test/checkout/CXP-…",
     "payment_id": null, "confirmed_at": null }
   ```
3. Provider now reports success (`fake.forced_status[ref]="SUCCEEDED"`).
   `GET /collections/1/?verify=1` → `confirm_collection` polls
   `verify_collection` → SUCCEEDED → `_book_receipt`.
   Resulting journal (`post_payment`):
   - Dr `1100`/deposit 50 000 · Cr `1200` AR 50 000 (fully allocated to invoice 7;
     `excess = 0`, no `2140` line).
   Intent → `status=SUCCEEDED`, `payment_id` set, `confirmed_at` stamped; invoice
   `amount_paid = 50 000`. (Asserted in `tests.py:144-150`.)

## 8. Gotchas / known limitations

> Hardening pass (2026-07-04) closed items 1–4, 6, 7 below. 5 and 8 remain open,
> as noted.

1. ✅ **Provider-reported settled amount is now booked.** `confirm_collection`
   adopts `settled = amount or intent.amount`; when the provider reports a positive
   amount that differs from the request it stashes `requested_amount` on metadata
   and books the settled figure (`services.py:214-220`); the webhook threads
   `parsed.amount` through (`webhooks.py:94`). A `0` report never overrides. Test:
   `test_settled_amount_overrides_requested`.

2. ✅ **Standalone (no-invoice) collections park as customer credit, not
   auto-settlement.** `_book_receipt` passes `auto_allocate=False` when there is no
   invoice (`services.py:259-264`), so the cash lands in `2140` instead of silently
   paying down the customer's oldest open invoices. Test:
   `test_standalone_receipt_parks_credit_not_auto_settling`.

3. ✅ **Customer-less collection is rejected at initiation.**
   `initiate_collection` raises `ValidationError` (400) before creating the intent
   or calling the provider when no customer is given/derivable (`services.py:77-78`).
   Test: `test_customerless_initiate_is_rejected`.

4. ✅ **"One active VA per customer per provider" is now enforced.** A partial
   `UniqueConstraint` on `(entity, provider, customer)` where `status=ACTIVE and
   customer not null` (`models.py:77-81`, migration `0003`) plus a friendly
   pre-flight guard in `create_virtual_account` (`services.py:131-136`). Test:
   `test_one_active_virtual_account_per_customer_provider`.

5. ⚠️ **`REFUNDED` is a dead state in this slice.** It is in the enum and the
   summary `group_counts` (`constants.py:53`, `views.py:168`) but no
   endpoint/service transitions into it. **Open — by design for now** (refunds are a
   later capability); worth ensuring the console does not advertise a refund action
   that no-ops.

6. ✅ **VA list now uses the standard envelope.** `GET /virtual-accounts/` routes
   through `_paginate`/`XVSPagination` (page size 25, real next/previous) with
   `kpis` injected onto `resp.data` (`views.py:239-242`) — consistent with every
   other list. Frontend-visible change: default page size 20 → 25. Test:
   `test_virtual_account_list_uses_standard_envelope`.

7. ✅ **A deposit on an INACTIVE VA is held, not auto-booked.** `_book_receipt`
   raises `PaymentStateError` when the linked VA is INACTIVE (`services.py:241-244`);
   the webhook path marks the event FAILED (retained/replayable) rather than posting
   to a deactivated NUBAN. Note: `set_virtual_account_status` is still local-only
   (no provider-side teardown — by design). Test:
   `test_inactive_virtual_account_deposit_is_held`.

8. ⚠️ **`search` on account_number is an FLS oracle.** `account_number` is
   FLS-masked in the serializer (§9), but the list view lets any
   `payments.virtual_account.view` holder filter by `search=<digits>` against
   `account_number` (`views.py:235-238`), so presence can be probed without the
   sensitive grant. **Open** — low severity; revisit in the settlement/FLS review.

## 9. Permissions & tenant isolation

RBAC keys, seeded by `seed_payments_permissions.py:26-33` and granted to
`xvs_super_admin` / `xvs_platform_admin`:
- `payments.collection.view` (NORMAL) — list/detail/summary.
- `payments.collection.create` (**CRITICAL**) — POST initiate.
- `payments.virtual_account.view` (NORMAL) — list/detail.
- `payments.virtual_account.create` (**SENSITIVE**) — POST provision.
- `payments.virtual_account.manage` (SENSITIVE) — PATCH status.
- `payments.virtual_account.view_sensitive` (SENSITIVE) — unmask VA funding
  number/name.

Verb correctness: POST paths take `create`, PATCH takes `manage`, reads take
`view` — via the `rbac_permission` property switching on `request.method`
(`views.py:99-103, 217-220, 287-290`). Every view class is
`IsAuthenticatedAndActive & HasRBACPermission`.

**Tenant isolation.** Every endpoint calls `resolve_entity(request)`
(`vs_finance/views.py:47-78`): holding a permission key is not enough — a non-CX
user is restricted to entities sourced from their school, and unknown/forbidden
entities both return **404** (no existence oracle). All querysets are
`.filter(entity=entity)` and detail lookups are `.filter(entity=entity, pk=pk)`
(`views.py:196, 224, 294`), so a `pk` from another tenant 404s. Body references
(`customer`/`invoice`/`deposit_account`/VA `customer`) are resolved **within the
entity** by `_entity_obj` (`views.py:58-75`), blocking cross-tenant
mass-assignment.

**FLS.** `VirtualAccountSerializer.read_permissions` masks `account_number` and
`account_name` unless the caller holds `payments.virtual_account.view_sensitive`
(`serializers.py:41-63`) — the list/detail views pass `context={"request": …}` so
the mixin can see the user. (Oracle caveat in §8.8.) `CollectionIntentSerializer`
exposes no PII beyond payer email/name it was given, and does **not** serialize
`metadata`/`raw_response` (`serializers.py:27-35`) — good; the raw PSP body stays
server-side.

## 10. Code map

- `models.py:33-168` — `VirtualAccount`, `CollectionIntent`.
- `constants.py:15-60,103-131` — providers, channels, collection statuses +
  terminal set, VA status, audit actions.
- `views.py:91-313` — collection list/create/summary/detail + VA list/create/detail
  views; `_entity_obj`/`_paginate` helpers (`views.py:49-75`).
- `services.py:55-241` — `initiate_collection`, `create_virtual_account`,
  `set_virtual_account_status`, `confirm_collection`, `_book_receipt`.
- `serializers.py:18-63` — `CollectionIntentSerializer`, `VirtualAccountSerializer`
  (+ FLS).
- `providers/base.py` — neutral `CheckoutResult` / `VirtualAccountResult` /
  `CollectionStatusResult`; `providers/registry.py` — name → client (test override);
  `providers/fake.py` — deterministic test provider.
- `audit.py` — immutable `PaymentEvent` writer (`record` / `record_rejection`).
- `vs_finance/receivables.py:226-398` — `post_payment` (the actual journal).

## 11. Test coverage & gaps

Baseline after hardening: **38 green** (`python manage.py test vs_payments
--settings=apps.settings.local`). Collections/VA-relevant:
- `CollectionTests`: initiate → PROCESSING + checkout + audit row; verify → books
  receipt & settles invoice; failed collection books nothing; **confirm
  idempotency**; plus the five hardening tests — `test_settled_amount_overrides_
  requested`, `test_standalone_receipt_parks_credit_not_auto_settling`,
  `test_customerless_initiate_is_rejected`,
  `test_one_active_virtual_account_per_customer_provider`,
  `test_inactive_virtual_account_deposit_is_held`.
- `PaymentsAPITests`: `test_initiate_collection_endpoint`,
  `test_collection_detail_verify_confirms`,
  `test_virtual_account_provision_list_and_status` (provision, paginated list +
  KPIs, status filter, PATCH deactivate, bogus-status 400, `account_number` visible
  *because* super-admin holds `view_sensitive`),
  `test_collections_filter_by_virtual_account`,
  `test_virtual_account_list_uses_standard_envelope`.

Gaps still open:
- **403 / permission-denied** — no test asserts a caller *without*
  `collection.create` / `virtual_account.create` gets 403.
- **Cross-tenant isolation** — no test that a `pk` or `?entity` from another
  tenant 404s on these routes.
- **FLS masking (negative case)** — no test that a caller *without*
  `view_sensitive` sees VA `account_number` stripped; and the §8.8 `search` oracle
  is unaddressed.
- **Empty-list shape** — `success_response` coerces `[]`→`{}`; the collections
  empty list envelope is unasserted.
</content>
</invoke>
