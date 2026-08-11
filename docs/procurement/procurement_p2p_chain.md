# procurement_p2p_chain

The procurement procure-to-pay chain covers the **approved buying commitment,
physical receipt, supplier bill, and cash settlement**: purchase order (PO) →
goods-received note (GRN) → vendor invoice → vendor payment. Routes are mounted at
`/v1/procurement/`; every route in this slice requires `?entity=<id|code>`.

---

## 1. What it is (and what it is NOT)

- A `PurchaseOrder` is the approved commitment to buy. It tracks ordered, received,
  and invoiced quantities but does not itself create a journal (`models.py:876-960`).
- A `GoodsReceivedNote` records accepted/rejected delivery quantities. Posting accepted
  value is the chain's first GL event: Dr expense/inventory, Cr GR/IR (`models.py:1030-1072`).
- A `VendorInvoice` prices the supplier bill, performs the PO/receipt/bill match, and on
posting creates the AP liability (`models.py:1129-1225`; `payables.py:62-115,149-323`).
- A `VendorPayment` moves the approved gross settlement out of AP, splits net cash and
  withholding tax, and allocates gross value to posted bills (`models.py:1285-1401`).

**This is not a purchasing-catalog or sourcing workflow.** Requisitions, RFQs, offers,
and awards are documented in `procurement_sourcing.md`; stock valuation and stock issues
belong to `procurement_inventory.md`. It is also not a replacement for the finance
posting engine: procurement builds source journals, while finance validates balance,
open periods, and posting (`purchasing.py:323-325`; `payables.py:296-307,477-489`).

## 2. Domain model

Money fields below are integer **kobo**. Quantities are `Decimal(14,4)` except the GRN
API currently restricts physical counts to whole units (`views/receiving.py:65-71`).

| Model | Key fields | Tenant/relationship rules |
|---|---|---|
| `PurchaseOrder` | vendor, optional requisition/contract, order/expected dates, terms, currency, subtotal/tax/total, shared `status`, workflow `approval_state` | Entity-protected document; vendor/requisition protected, contract `SET_NULL`; indexed by entity/status, vendor, entity/date (`models.py:876-929`) |
| `PurchaseOrderLine` | source requisition line, expense account, quantity/unit price, net/tax, service-owned received/invoiced quantities, cost center | Cascades with PO; account/source/cost center protected; PO and expense indexes (`models.py:963-1005`) |
| `GoodsReceivedNote` | vendor, optional PO, received date/by, reference/narration, accepted ex-tax `total_value`, journal, DRAFT/POSTED status | Entity-protected document; vendor/PO/journal protected; entity/status, vendor, entity/date indexes (`models.py:1030-1064`) |
| `GoodsReceivedNoteLine` | optional PO line/stock item, expense account, accepted/rejected/expected quantities, unit price/value, cost center | Cascades with GRN; source/account/stock/cost-center references protected (`models.py:1075-1119`) |
| `VendorInvoice` | vendor, optional PO, invoice/due dates, vendor reference, subtotal/tax/total, amount paid, match/payment/approval states, journal | Entity-protected document; vendor/PO/journal protected; non-blank vendor reference is case-insensitively unique per entity/vendor in the database; entity/status, entity/payment state, vendor, entity/date indexes (`models.py:1129-1198`) |
| `VendorInvoiceLine` | optional PO/GRN line, expense/tax, quantity/unit price, net/tax, cost center | Cascades with invoice; source/account/tax/cost-center references protected (`models.py:1228-1270`) |
| `VendorPayment` | vendor/date/method, approval state, gross/WHT/net/allocated kobo, payment account, WHT tax code, journal | Entity-protected document; database checks require positive gross and WHT/allocation within gross; entity/status, entity/approval, vendor, entity/date indexes (`models.py:1285-1354`) |
| `VendorPaymentAllocation` | payment, vendor invoice, gross amount applied | Payment cascades; invoice protected; unique `(payment, vendor_invoice)` and non-negative amount (`models.py:1363-1398`) |

PO, invoice, and payment approval are overlays owned by `vs_workflow`; ledger `status`
remains independently authoritative (`models.py:918-922,1170-1178,1308-1316`). A posted
invoice can therefore remain UNPAID/PARTIAL/PAID without changing its POSTED status
(`models.py:1210-1225`).

## 3. Endpoint map

Request bodies list only fields the view actually reads. List endpoints return the
standard paginated `{pagination, data}` envelope (`views/base.py:281-298`).

| Method + path | permission key | what it does | request body / query fields actually read | response shape |
|---|---|---|---|---|
| `GET /purchase-orders/` | `procurement.purchase_order.view` | List/search POs with derived receipt stage | Query `status`, `vendor`, `search` | Paginated PO headers/progress; no nested document arrays (`views/orders.py:129-155,202-218`; `serializers.py:835-849`) |
| `POST /purchase-orders/` | `procurement.purchase_order.create` | Create a priced DRAFT PO from an approved requisition | `requisition`, `vendor`, `order_date`, `expected_date?`, `delivery_address?`, `payment_terms?`, `currency?`, `contract?` | `201` full PO + lines (`views/orders.py:220-244`; `serializers.py:776-812`) |
| `GET /purchase-orders/summary/` | `procurement.purchase_order.view` | Entity-wide issued-PO pipeline KPIs | - | `{as_of, open, partially_received, awaiting_receipt, po_value_mtd}` (`views/orders.py:158-200,317-324`) |
| `GET /purchase-orders/<pk>/` | `procurement.purchase_order.view` | Read source, progress, receipts, invoices, and workflow id | - | Full PO + `workflow_instance_id` (`views/orders.py:260-270`; `serializers.py:776-812`) |
| `PATCH /purchase-orders/<pk>/` | `procurement.purchase_order.update` | Edit only mutable DRAFT commercial terms; lines stay fixed | `vendor?`, `order_date?`, `expected_date?`, `delivery_address?`, `payment_terms?`, `contract?` | Updated full PO (`views/orders.py:272-314`) |
| `POST /purchase-orders/<pk>/submit/` | `procurement.purchase_order.submit` | Submit PO to workflow; does not post | - | Workflow id/status, approval state, document (`views/requisitions.py:363-375`) |
| `GET /goods-receipts/` | `procurement.goods_receipt.view` | List entity GRNs | Query `status` | Paginated receipt headers/progress, no nested lines (`views/receiving.py:123-144`; `serializers.py:947-955`) |
| `POST /goods-receipts/` | `procurement.goods_receipt.create` | Create an unposted physical-receipt snapshot | `vendor`, `purchase_order?`, `received_date`, `reference?`, `narration?`; `lines[]`: `po_line?`, `line_no?`, `description?`, `expense_account?`, `cost_center?`, `accepted_qty?`, `rejected_qty?`, `unit_price?` | `201` GRN + lines, including additive `cost_center_id/code` (`views/receiving.py:56-120,146-176`; `serializers.py:856-875`) |
| `GET /goods-receipts/<pk>/` | `procurement.goods_receipt.view` | Read one GRN | - | GRN + derived receipt status/counts + lines (`views/receiving.py:178-198`; `serializers.py:856-945`) |
| `PATCH /goods-receipts/<pk>/` | `procurement.goods_receipt.update` | Edit DRAFT header; `lines` fully replaces receipt lines | `received_date?`, `reference?`, `narration?`, `lines?` using POST line fields | Updated GRN (`views/receiving.py:200-225`) |
| `POST /goods-receipts/<pk>/post/` | `procurement.goods_receipt.post` | Post accepted value and advance PO receipt quantities | - | Posted GRN + journal id and receipt lines (`views/receiving.py:228-252`) |
| `GET /vendor-invoices/` | `procurement.vendor_invoice.view` | List/search bills by independent lifecycle fields | Query `status`, `payment_status`, `match_status`, `vendor`, `display_status`, `search` | Paginated invoice headers; no lines (`views/receiving.py:456-483`; `serializers.py:1034-1040`) |
| `POST /vendor-invoices/` | `procurement.vendor_invoice.create` | Create and price a DRAFT bill | `vendor`, `purchase_order?`, `invoice_date`, `due_date?`, `currency?`, `vendor_reference?`, `narration?`; `lines[]`: `po_line?`, `grn_line?`, `line_no?`, `description?`, `expense_account?`, `cost_center?`, `quantity?`, `unit_price?`, `tax_code?` | `201` invoice + match/payment/posting/activity overlays; line response includes additive `cost_center_id/code` (`views/receiving.py:332-388,485-518`; `serializers.py:964-978`) |
| `GET /vendor-invoices/summary/` | `procurement.vendor_invoice.view` | Bill-review and overdue KPIs | - | `{as_of, under_review, approved, overdue, disputed}` (`views/receiving.py:521-538`) |
| `GET /vendor-invoices/<pk>/` | `procurement.vendor_invoice.view` | Read match comparisons, allocations, posting lines, activity | - | Full invoice detail overlay (`views/receiving.py:391-453,541-555`) |
| `PATCH /vendor-invoices/<pk>/` | `procurement.vendor_invoice.update` | Edit an unsubmitted/rejected DRAFT; optional `lines` fully replaces lines | `vendor?`, `purchase_order?`, `invoice_date?`, `due_date?`, `vendor_reference?`, `narration?`, `lines?` using POST line fields | Updated full invoice (`views/receiving.py:557-604`) |
| `POST /vendor-invoices/<pk>/match/` | `procurement.vendor_invoice.match` | Reprice and run three-way match without GL posting | - | Full invoice with match result/comparisons (`views/receiving.py:607-627`) |
| `POST /vendor-invoices/<pk>/submit/` | `procurement.vendor_invoice.submit` | Reprice/match, then submit current evidence to workflow | - | Workflow id/status, approval state, document (`views/requisitions.py:378-397`) |
| `POST /vendor-invoices/<pk>/post/` | `procurement.vendor_invoice.post`; additionally `procurement.vendor_invoice.override_variance` when overriding | Post an approved bill; optionally override a blocking match | `allow_variance?` (JSON boolean only) | Posted full invoice detail (`views/receiving.py:630-669`) |
| `GET /vendor-payments/` | `procurement.vendor_payment.view` | List/search payment instructions | Query `status`, `approval_state`, `search` | Paginated payment headers with allocations (`views/vendor_payments.py:165-185`; `serializers.py:1071-1130`) |
| `POST /vendor-payments/` | `procurement.vendor_payment.create` | Create a gated DRAFT allocation plan; server derives money | `vendor`, `bank_account`, `payment_date`, `method?`, `wht_amount?`, `wht_tax_code?`, `reference?`, `narration?`; `allocations[]`: `vendor_invoice`, `amount` | `201` payment detail + workflow/posting/activity overlays (`views/vendor_payments.py:187-214`) |
| `GET /vendor-payments/eligible-invoices/` | `procurement.vendor_payment.view` | Return at most 100 posted open bills, oldest due first | Query `vendor?` | Array of invoice settlement snapshots (`views/vendor_payments.py:217-239`) |
| `GET /vendor-payments/<pk>/` | `procurement.vendor_payment.view` | Read plan/settlement, workflow, journal, activity | - | Full payment detail overlay (`views/vendor_payments.py:132-162,242-257`) |
| `PATCH /vendor-payments/<pk>/` | `procurement.vendor_payment.update` | Replace an unsubmitted/rejected DRAFT plan and derived totals | Same header fields as POST; `allocations` required | Updated payment detail (`views/vendor_payments.py:259-295`) |
| `POST /vendor-payments/<pk>/submit/` | `procurement.vendor_payment.submit` | Submit a DRAFT allocation plan to workflow | - | Document, workflow id, approval state (`views/vendor_payments.py:298-315`) |
| `POST /vendor-payments/<pk>/post/` | `procurement.vendor_payment.post` | Post the approved persisted plan and settle its invoices | - | Posted payment detail (`views/vendor_payments.py:318-336`) |
| `POST /vendor-payments/<pk>/cancel/` | `procurement.vendor_payment.cancel` | Cancel a non-pending, unposted DRAFT | - | Cancelled payment (`views/vendor_payments.py:339-354`) |
| `POST /vendor-payments/<pk>/reverse/` | `procurement.vendor_payment.reverse` | Reverse a posted payment and restore invoice balances | `date?` | Reversed payment detail (`views/vendor_payments.py:357-372`) |

The cross-cutting `/approvals/` endpoints act on PO, invoice, and payment workflow
instances; they are documented once with sourcing's approval model rather than repeated
as document CRUD here (`urls.py:91-99`; `views/requisitions.py:400-592`).

## 4. Lifecycle / state machine

```text
Purchase order:
DRAFT + NOT_SUBMITTED ─submit─▶ DRAFT + PENDING approval
       ▲                          ├─workflow approve─▶ APPROVED + APPROVED approval
       └─workflow withdraw───────┘└─workflow reject─▶ DRAFT + REJECTED approval

Goods receipt:
DRAFT ─post─▶ POSTED

Vendor invoice:
DRAFT ─match─▶ DRAFT + AUTO_MATCHED / PRICE_VARIANCE /
                       UNDER_RECEIVED / OVER_BILLED
      └─submit─▶ PENDING approval ─approve─▶ APPROVED overlay
                                             └─post─▶ POSTED
POSTED settlement overlay: UNPAID ─allocation─▶ PARTIAL ─allocation─▶ PAID

Vendor payment:
DRAFT ─submit─▶ PENDING approval ─approve─▶ APPROVED overlay ─post─▶ POSTED
   └─cancel────────────────────────────────────────────────────────▶ CANCELLED
POSTED ─reverse─▶ REVERSED (original journal reversed; allocation history retained)
```

Workflow approval changes PO ledger status to APPROVED, while invoice/payment approval
leaves the document DRAFT and changes the approval overlay; their separate post endpoint
creates the journal (`purchasing.py:140-159`; `approvals.py:179-239`). Invoice submission
freezes priced/matched evidence, but posting reprices and rematches under invoice and
PO-line locks before writing anything (`views/requisitions.py:378-397`;
`payables.py:149-193`). Payment posting similarly locks payment, vendor, persisted plan,
and invoice targets before checking approval and balances (`payables.py:355-442`).

## 5. Calculations

- PO/invoice/GRN line net: `round_half_up(quantity × unit_price)` kobo through the shared
  `compute_line_net` helper (`purchasing.py:57-72,282-286`; `payables.py:48-58`).
  Example: `5 × 200,000 = 1,000,000` kobo.
- Line tax: `round_half_up(net × rate_bps ÷ 10,000)`; PO and invoice pricing both call
  the shared `compute_tax` helper (`purchasing.py:65-72`; `payables.py:52-58`). At 7.5%,
  `1,000,000 × 750 ÷ 10,000 = 75,000` kobo.
- Header gross: `subtotal = Σ line.net`; `tax_total = Σ line.tax`;
  `total = subtotal + tax_total` (`models.py:931-941,1198-1208`). The example bill is
  `1,000,000 + 75,000 = 1,075,000` kobo.
- Receipt value: `Σ round_half_up(accepted_qty × unit_price)`; rejected quantity never
  contributes (`models.py:1066-1072`; `purchasing.py:267-298`).
- PO receipt/invoice percentages: `Σ received_or_invoiced quantity ÷ Σ ordered quantity ×
  100`, with the shared percentage helper's decimal rounding (`models.py:943-955`).
- Three-way match per PO line uses `billed_cumulative = posted invoiced_qty + current
  invoice quantity`; over ordered → OVER_BILLED, over received → UNDER_RECEIVED, different
  unit price → PRICE_VARIANCE, otherwise AUTO_MATCHED (`payables.py:62-115`).
- Receipt/PO-backed invoice posting separates clearing basis from billed price:
  `basis = round_half_up(quantity × linked GRN unit price)` when a posted GRN line is
  linked, otherwise `round_half_up(quantity × PO unit price)`; `PPV = invoice net −
  basis`. Positive PPV is an unfavorable debit, negative PPV a favorable credit, to
  seeded expense account `5160 Purchase Price Variance`. Direct bills with neither
  source have no PPV (`payables.py:205-245`; `seed.py:54-57,98-112`).
- Invoice balance: `balance_due = total − amount_paid`; payment state is UNPAID at zero,
  PAID at `amount_paid >= total`, otherwise PARTIAL (`models.py:1193-1225`).
- Payment values: `gross = Σ requested allocation amounts`; `net = gross − WHT`;
  `unallocated = gross − allocated` (`views/vendor_payments.py:187-211`;
  `models.py:1356-1360`). Example: gross `1,075,000` − WHT `50,000` = bank outflow
  `1,025,000` kobo.
- Explicit allocation is capped by the selected posted invoice's current balance and the
  payment's remaining gross. Automatic service allocation is oldest due date, invoice
  date, then id (`payables.py:503-595`).
- PO summary MTD change is `(current comparable MTD − prior comparable MTD) ÷ prior ×
  100`, one decimal, or `null` when prior is zero (`views/orders.py:158-200`).

## 6. What posting does to the ledger

### Purchase order

Approval creates **no journal**. The PO remains a commitment with priced lines and
workflow evidence (`purchasing.py:140-159`).

Approved purchase orders can be emailed to vendors through a separate, audited delivery
flow. A buyer may schedule the email while raising the draft for approval, but the
delivery remains `AWAITING_APPROVAL` and is released only after full workflow approval
commits. Rejection, withdrawal, or workflow cancellation cancels that intent. Email or
PDF failures do not undo approval; they become a visible `FAILED` delivery that an
authorised user can retry as a new audit record.

Recipient order is explicit active contacts marked `receives_purchase_orders`, then the
active primary contact, then the vendor master email. The message includes branded HTML,
the temporary procurement CC configured by `PROCUREMENT_VENDOR_EMAIL_CC`, and a generated
PDF containing the delivery address, expected date, payment terms, line summary, totals,
buyer note, and buyer contact. Manual send and retry require
`procurement.purchase_order.email_vendor`. Ordinary purchase-order reads expose delivery
status and counts, while the exact email addresses are returned only by the permission-
gated preview endpoint.

### Goods receipt

For each accepted non-stock line, grouped by `(account, cost center)`:

```text
Dr line expense account (or stock inventory account)   accepted net value
    Cr 2150 GR/IR clearing                              total accepted net value
```

Rejected units do not post. The same transaction posts the journal, advances PO-line
`received_qty`, updates stock movements for stock-backed lines, links the journal, marks
the GRN POSTED, and records audit evidence (`purchasing.py:250-399`). A non-stock expense
debit carries the line cost center into `JournalLine`; inventory and GR/IR control lines
remain unallocated (`purchasing.py:284-361`).

### Vendor invoice

For receipt/PO-backed lines:

```text
Dr 2150 GR/IR clearing                receipt/PO basis
Dr 5160 Purchase Price Variance       unfavorable difference
    Cr 5160 Purchase Price Variance   favorable difference
Dr tax_code.paid_account              recoverable input tax
    Cr vendor.payable_account         invoice gross
```

Only one PPV side appears for each cost-center group. For non-PO lines with no receipt
evidence, net still debits the line expense account and no PPV account is resolved. A
line linked to a posted direct GRN clears GR/IR at that GRN's historical price even
without a PO. Direct expense and PPV lines carry cost center; GR/IR, input-tax, AP, and
inventory controls do not (`payables.py:205-292`). After the balanced journal posts, PO
`invoiced_qty` advances and the bill becomes POSTED (`payables.py:294-323`).

GR/IR aging and its GRN/PO-line drill-downs use that same receipt-first, PO-fallback
clearing basis-not billed net-so normal PPV does not appear as an open GR/IR item. Their
queries eager-load the source evidence rather than introducing per-line lookups
(`reports.py:284-380,499-550,604-683,708-783`).

### Vendor payment

```text
Dr vendor.payable_account             gross settled
    Cr payment bank/cash account      gross − WHT
    Cr tax-code WHT account or 2300   WHT retained
```

Posting then recreates the approved draft plan as posted allocations, advances each
invoice's `amount_paid`/payment state, and stores `allocated_amount`; allocation creates
no second journal (`payables.py:355-500,503-595`). Reversal uses finance's reversing
journal and subtracts historical allocations from invoice settlement totals while
retaining the allocation rows as history (`payables.py:598-637`).

## 7. Worked example

An approved PO has five chairs at `200000` kobo with VAT 7.5%. A receiver records all
five accepted:

```json
POST /v1/procurement/goods-receipts/?entity=LEKKI
{
  "vendor": "CHAIRS01",
  "purchase_order": 71,
  "received_date": "2026-07-22",
  "lines": [{
    "po_line": 181,
    "accepted_qty": 5,
    "rejected_qty": 0,
    "unit_price": 200000
  }]
}
```

The DRAFT response derives `value_amount=1000000`. Posting produces:

```text
Dr 5100 Furniture expense   1,000,000
    Cr 2150 GR/IR clearing  1,000,000
```

The supplier bill references PO line 181 and quantity 5. Pricing derives subtotal
`1,000,000`, VAT `75,000`, total `1,075,000`; matching is AUTO_MATCHED. After approval,
invoice posting produces:

```text
Dr 2150 GR/IR clearing      1,000,000
Dr 1300 Input VAT              75,000
    Cr 2100 AP control      1,075,000
```

If the same five units are billed at `225000` each, the receipt basis remains
`1,000,000`, invoice net is `1,125,000`, and unfavorable PPV is `125,000`. At 7.5% VAT
the journal is Dr GR/IR `1,000,000`, Dr 5160 PPV `125,000`, Dr input VAT `84,375`, Cr AP
`1,209,375`; GR/IR still clears to zero.

A payment draft selects that bill for `1,075,000` gross and withholds `50,000`:

```json
POST /v1/procurement/vendor-payments/?entity=LEKKI
{
  "vendor": "CHAIRS01",
  "bank_account": 4,
  "payment_date": "2026-07-30",
  "wht_amount": 50000,
  "allocations": [{"vendor_invoice": 91, "amount": 1075000}]
}
```

The server derives net `1,025,000`. Approval plus posting produces Dr AP `1,075,000`,
Cr bank `1,025,000`, Cr WHT payable `50,000`, and marks invoice 91 PAID. The arithmetic
and journal path are exercised end-to-end in
`tests.py:1022-1042,1315-1333,1766-1788,2020-2072`.

## 8. Gotchas / known limitations

- ✅ **Direct receipt-backed bills clear GR/IR.** A non-PO GRN still posts Dr expense /
  Cr GR/IR, but an invoice line linked to that posted GRN now debits GR/IR rather than
  booking the expense twice. Generic non-PO/non-GRN bills remain direct-expense bills
  (`views/receiving.py:348-383`; `payables.py:221-245`).
- ✅ **GRN posting is serialized and quantity-safe.** The worker locks/re-reads the GRN,
  then locks referenced PO lines in stable id order, aggregates duplicate receipt lines,
  and rechecks live remaining quantity before any journal or counter update. Concurrent
  duplicate posts create one journal, and competing receipts cannot over-receive
  (`purchasing.py:267-318,368-399`).
- ✅ **Receipt and bill quantities use strict model-bound validation.** GRN counts must be
  non-negative finite whole units; invoice quantities must be positive and finite; both
  reject values outside `Decimal(14,4)` before replacement arithmetic, so malformed
  create/PATCH requests roll back with 400 rather than 500
  (`views/base.py:129-175`; `views/receiving.py:56-111,332-385`).
- ✅ **PO-backed receiving requires an approved PO.** API creation rejects a non-approved
  selected PO for fast feedback, and the locked posting service repeats the authoritative
  gate so direct service callers cannot bypass it. Direct GRNs remain allowed
  (`views/receiving.py:146-176`; `purchasing.py:255-281`).
- ✅ **Blocking-match override is strict, separately authorized, and audited.**
  `allow_variance` accepts only a real JSON boolean; `true` requires the additional
  tenant/branch-aware CRITICAL `procurement.vendor_invoice.override_variance` permission.
  Successful posting audit metadata records both the request and whether a blocking
  variance was actually overridden (`views/receiving.py:630-669`;
  `payables.py:310-318`; `management/commands/seed_procurement_permissions.py:46-47`).
- ✅ **Cost-center ownership now reaches departmental actuals.** PO-backed receipts copy
  the PO-line center; invoices prefer PO-line then GRN-line ownership; direct lines may
  provide an active entity-scoped center. Explicit source mismatches are rejected.
  Non-stock expense and PPV journal lines retain the center while inventory, GR/IR, tax,
  and AP controls deliberately drop it (`views/receiving.py:89-105,358-383`;
  `purchasing.py:284-361`; `payables.py:210-287`).
- ✅ **Vendor invoice references are race-safe.** Non-blank references are
  case-insensitively unique per `(entity, vendor)` in the database. Create/PATCH keep the
  fast pre-check but translate a named-constraint race into the same field-level 400.
  Migration `0015` refuses historical collisions with actionable examples rather than
  silently rewriting supplier evidence (`models.py:1183-1192`;
  `views/receiving.py:295-329,485-515,557-600`;
  `migrations/0015_vendorinvoice_reference_ci.py:6-49`).
- **Stock-backed receiving is not writable through this API.** The line model and posting
  service support `stock_item`, but GRN create/PATCH never read or set it. This boundary
  is traced and resolved with the inventory slice (`models.py:1093-1097`;
  `views/receiving.py:56-111`; `purchasing.py:283-399`).
- ✅ **Price differences post to PPV instead of remaining in GR/IR.** Receipt evidence is
  the first clearing basis, PO price is the fallback, and actual-minus-basis posts Dr/Cr
  to seeded account `5160`. The PPV account is resolved only for a non-zero difference;
  equal-price and direct-expense bills remain independent of it. GR/IR reports use the
  identical basis (`payables.py:205-282`; `reports.py:284-380`;
  `seed.py:54-57,98-112`).
- **Justified by design:** rejected delivery quantity is evidence only and does not
  advance PO received quantity, value inventory, or post to the GL, allowing a later
  replacement delivery (`purchasing.py:283-335,368-386`).
- **Justified by design:** payment allocations are gross AP settlement, not the net bank
  outflow. WHT is part of the invoice settlement even though it is remitted separately
  (`models.py:1285-1291,1363-1368`; `payables.py:355-500`).

## 9. Permissions & tenant isolation

Every view inherits authenticated-user RBAC and resolves the selected ledger entity.
Document reads/writes filter by that entity; vendors, accounts, taxes, bank accounts,
PO/GRN lines, invoices, and allocation targets are re-resolved inside it. Foreign ids
therefore return missing/invalid rather than exposing or mutating another entity
(`views/base.py:31-105,237-269,301-318`; `views/receiving.py:56-105,254-388`;
`views/vendor_payments.py:45-57,82-109`).

Line cost centers use the shared active, entity-scoped id/code resolver; a foreign or
inactive center is a 400. Invoice-reference isolation is also enforced below the view by
the database's entity/vendor-scoped conditional unique constraint (`views/base.py:75-90`;
`models.py:1183-1192`).

The seeded matrix separates view/create/update/submit/post for each money document and
uses a distinct `vendor_invoice.override_variance` authority for blocking-match bypass.
Goods-receipt post, vendor-invoice post, and every vendor-payment mutation are CRITICAL;
the variance override is also CRITICAL. PO submit, receipt create/update, and invoice
create/update/submit/match are SENSITIVE
(`management/commands/seed_procurement_permissions.py:29-50`). Payment creation and
posting also recheck active/KYC/hold state and lock vendor/invoice rows at the accounting
boundary (`views/vendor_payments.py:60-71`; `payables.py:355-442`).

There is currently no field-level masking on these serializers. They expose operational
references, totals, account ids/codes, allocations, journal lines, and human-readable
activity, but not raw audit metadata (`serializers.py:740-1130`;
`views/receiving.py:391-453`; `views/vendor_payments.py:122-162`).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py` | PO, GRN, invoice, payment, line/allocation storage and derived totals |
| `views/orders.py` | PO CRUD, filters, summary, contract validation |
| `views/receiving.py` | GRN/invoice CRUD, line validation, matching/post endpoints, detail overlays |
| `views/vendor_payments.py` | Payment plan CRUD, vendor/bank gates, eligible bills, post/cancel/reverse |
| `purchasing.py` | PO creation/pricing/approval and GRN accounting/quantity effects |
| `payables.py` | Invoice pricing/matching/posting and payment posting/allocation/reversal |
| `reports.py` | AP/GR/IR read models; GR/IR detail uses the invoice-posting clearing basis |
| `approvals.py` / `workflow_handlers.py` | Threshold workflows and terminal document effects |
| `serializers.py` | Public P2P response shapes and display-state overlays |
| `constants.py` | Match/payment states and 2150 GR/IR / 2300 WHT / 5160 PPV account codes |
| `migrations/0015_vendorinvoice_reference_ci.py` | Historical collision guard + database invoice-reference constraint |
| `vs_finance/seed.py` | Seeds 5160 PPV under Expenses and maps it to IFRS Cost of Sales |
| `core/management/commands/seed_actions.py` | Canonical global permission-action vocabulary |
| `urls.py` | `/v1/procurement/` route map |
| `management/commands/seed_procurement_permissions.py` | RBAC registry/sensitivity/platform grants |

## 11. Test coverage & gaps

The current procurement suite is **233 green**. P2P service/API tests cover GRN whole,
finite, precision, and remainder validation, DRAFT edits, update permission, approved-PO
gating, expense→GR/IR posting, duplicate-post and competing-receipt PostgreSQL races;
invoice approval, split-line aggregation, GR/IR clearing, input VAT, blocking overbill
and under-receipt, direct-GRN clearing, strict/authorized/audited variance override,
cost-center source/direct validation and journal carry/drop, database/API
case-insensitive reference uniqueness, favorable/unfavorable/direct-GRN PPV and lazy
account resolution, PPV-aware GR/IR aging/detail, invoice view permission and
cross-entity detail; payment WHT split,
approval, plan validation, reversal, mutation permissions, cross-entity detail, draft
plans, partial settlement and held vendors; AP reconciliation; and the full PR-to-payment
chain (`tests.py:830-2072,2997-3056`). Purchase-order console and workflow tests cover response
data, filters/KPIs, permission gates, entity isolation, workflow routing, and terminal
approval effects (`tests.py:5049-5616`).

The remaining open §8 implementation item is stock-item receipt input, intentionally
deferred to the inventory slice. Empty-list envelope assertions exist broadly in the
procurement console suite but are not explicit for every one of the four P2P list routes.
