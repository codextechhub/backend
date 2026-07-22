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
  posting creates the AP liability (`models.py:1129-1225`; `payables.py:62-115,146-275`).
- A `VendorPayment` moves the approved gross settlement out of AP, splits net cash and
  withholding tax, and allocates gross value to posted bills (`models.py:1285-1401`).

**This is not a purchasing-catalog or sourcing workflow.** Requisitions, RFQs, offers,
and awards are documented in `procurement_sourcing.md`; stock valuation and stock issues
belong to `procurement_inventory.md`. It is also not a replacement for the finance
posting engine: procurement builds source journals, while finance validates balance,
open periods, and posting (`purchasing.py:323-325`; `payables.py:254-266,398-455`).

## 2. Domain model

Money fields below are integer **kobo**. Quantities are `Decimal(14,4)` except the GRN
API currently restricts physical counts to whole units (`views/receiving.py:65-71`).

| Model | Key fields | Tenant/relationship rules |
|---|---|---|
| `PurchaseOrder` | vendor, optional requisition/contract, order/expected dates, terms, currency, subtotal/tax/total, shared `status`, workflow `approval_state` | Entity-protected document; vendor/requisition protected, contract `SET_NULL`; indexed by entity/status, vendor, entity/date (`models.py:876-929`) |
| `PurchaseOrderLine` | source requisition line, expense account, quantity/unit price, net/tax, service-owned received/invoiced quantities, cost center | Cascades with PO; account/source/cost center protected; PO and expense indexes (`models.py:963-1005`) |
| `GoodsReceivedNote` | vendor, optional PO, received date/by, reference/narration, accepted ex-tax `total_value`, journal, DRAFT/POSTED status | Entity-protected document; vendor/PO/journal protected; entity/status, vendor, entity/date indexes (`models.py:1030-1064`) |
| `GoodsReceivedNoteLine` | optional PO line/stock item, expense account, accepted/rejected/expected quantities, unit price/value, cost center | Cascades with GRN; source/account/stock/cost-center references protected (`models.py:1075-1119`) |
| `VendorInvoice` | vendor, optional PO, invoice/due dates, vendor reference, subtotal/tax/total, amount paid, match/payment/approval states, journal | Entity-protected document; vendor/PO/journal protected; entity/status, entity/payment state, vendor, entity/date indexes (`models.py:1129-1191`) |
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
| `GET /purchase-orders/summary/` | `procurement.purchase_order.view` | Entity-wide issued-PO pipeline KPIs | — | `{as_of, open, partially_received, awaiting_receipt, po_value_mtd}` (`views/orders.py:158-200,317-324`) |
| `GET /purchase-orders/<pk>/` | `procurement.purchase_order.view` | Read source, progress, receipts, invoices, and workflow id | — | Full PO + `workflow_instance_id` (`views/orders.py:260-270`; `serializers.py:776-812`) |
| `PATCH /purchase-orders/<pk>/` | `procurement.purchase_order.update` | Edit only mutable DRAFT commercial terms; lines stay fixed | `vendor?`, `order_date?`, `expected_date?`, `delivery_address?`, `payment_terms?`, `contract?` | Updated full PO (`views/orders.py:272-314`) |
| `POST /purchase-orders/<pk>/submit/` | `procurement.purchase_order.submit` | Submit PO to workflow; does not post | — | Workflow id/status, approval state, document (`views/requisitions.py:363-375`) |
| `GET /goods-receipts/` | `procurement.goods_receipt.view` | List entity GRNs | Query `status` | Paginated receipt headers/progress, no nested lines (`views/receiving.py:112-130`; `serializers.py:947-955`) |
| `POST /goods-receipts/` | `procurement.goods_receipt.create` | Create an unposted physical-receipt snapshot | `vendor`, `purchase_order?`, `received_date`, `reference?`, `narration?`; `lines[]`: `po_line?`, `line_no?`, `description?`, `expense_account?`, `accepted_qty?`, `rejected_qty?`, `unit_price?` | `201` GRN + lines (`views/receiving.py:53-102,132-160`) |
| `GET /goods-receipts/<pk>/` | `procurement.goods_receipt.view` | Read one GRN | — | GRN + derived receipt status/counts + lines (`views/receiving.py:172-178`; `serializers.py:856-945`) |
| `PATCH /goods-receipts/<pk>/` | `procurement.goods_receipt.update` | Edit DRAFT header; `lines` fully replaces receipt lines | `received_date?`, `reference?`, `narration?`, `lines?` using POST line fields | Updated GRN (`views/receiving.py:180-206`) |
| `POST /goods-receipts/<pk>/post/` | `procurement.goods_receipt.post` | Post accepted value and advance PO receipt quantities | — | Posted GRN + journal id and receipt lines (`views/receiving.py:209-228`) |
| `GET /vendor-invoices/` | `procurement.vendor_invoice.view` | List/search bills by independent lifecycle fields | Query `status`, `payment_status`, `match_status`, `vendor`, `display_status`, `search` | Paginated invoice headers; no lines (`views/receiving.py:398-425`; `serializers.py:1034-1040`) |
| `POST /vendor-invoices/` | `procurement.vendor_invoice.create` | Create and price a DRAFT bill | `vendor`, `purchase_order?`, `invoice_date`, `due_date?`, `currency?`, `vendor_reference?`, `narration?`; `lines[]`: `po_line?`, `grn_line?`, `line_no?`, `description?`, `expense_account?`, `quantity?`, `unit_price?`, `tax_code?` | `201` invoice + match/payment/posting/activity overlays (`views/receiving.py:287-330,427-454`) |
| `GET /vendor-invoices/summary/` | `procurement.vendor_invoice.view` | Bill-review and overdue KPIs | — | `{as_of, under_review, approved, overdue, disputed}` (`views/receiving.py:457-474`) |
| `GET /vendor-invoices/<pk>/` | `procurement.vendor_invoice.view` | Read match comparisons, allocations, posting lines, activity | — | Full invoice detail overlay (`views/receiving.py:333-396,477-491`) |
| `PATCH /vendor-invoices/<pk>/` | `procurement.vendor_invoice.update` | Edit an unsubmitted/rejected DRAFT; optional `lines` fully replaces lines | `vendor?`, `purchase_order?`, `invoice_date?`, `due_date?`, `vendor_reference?`, `narration?`, `lines?` using POST line fields | Updated full invoice (`views/receiving.py:493-529`) |
| `POST /vendor-invoices/<pk>/match/` | `procurement.vendor_invoice.match` | Reprice and run three-way match without GL posting | — | Full invoice with match result/comparisons (`views/receiving.py:532-552`) |
| `POST /vendor-invoices/<pk>/submit/` | `procurement.vendor_invoice.submit` | Reprice/match, then submit current evidence to workflow | — | Workflow id/status, approval state, document (`views/requisitions.py:378-397`) |
| `POST /vendor-invoices/<pk>/post/` | `procurement.vendor_invoice.post` | Post an approved bill; optionally override a blocking match | `allow_variance?` | Posted full invoice detail (`views/receiving.py:555-577`) |
| `GET /vendor-payments/` | `procurement.vendor_payment.view` | List/search payment instructions | Query `status`, `approval_state`, `search` | Paginated payment headers with allocations (`views/vendor_payments.py:165-185`; `serializers.py:1071-1130`) |
| `POST /vendor-payments/` | `procurement.vendor_payment.create` | Create a gated DRAFT allocation plan; server derives money | `vendor`, `bank_account`, `payment_date`, `method?`, `wht_amount?`, `wht_tax_code?`, `reference?`, `narration?`; `allocations[]`: `vendor_invoice`, `amount` | `201` payment detail + workflow/posting/activity overlays (`views/vendor_payments.py:187-214`) |
| `GET /vendor-payments/eligible-invoices/` | `procurement.vendor_payment.view` | Return at most 100 posted open bills, oldest due first | Query `vendor?` | Array of invoice settlement snapshots (`views/vendor_payments.py:217-239`) |
| `GET /vendor-payments/<pk>/` | `procurement.vendor_payment.view` | Read plan/settlement, workflow, journal, activity | — | Full payment detail overlay (`views/vendor_payments.py:132-162,242-257`) |
| `PATCH /vendor-payments/<pk>/` | `procurement.vendor_payment.update` | Replace an unsubmitted/rejected DRAFT plan and derived totals | Same header fields as POST; `allocations` required | Updated payment detail (`views/vendor_payments.py:259-295`) |
| `POST /vendor-payments/<pk>/submit/` | `procurement.vendor_payment.submit` | Submit a DRAFT allocation plan to workflow | — | Document, workflow id, approval state (`views/vendor_payments.py:298-315`) |
| `POST /vendor-payments/<pk>/post/` | `procurement.vendor_payment.post` | Post the approved persisted plan and settle its invoices | — | Posted payment detail (`views/vendor_payments.py:318-336`) |
| `POST /vendor-payments/<pk>/cancel/` | `procurement.vendor_payment.cancel` | Cancel a non-pending, unposted DRAFT | — | Cancelled payment (`views/vendor_payments.py:339-354`) |
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
`payables.py:146-192`). Payment posting similarly locks payment, vendor, persisted plan,
and invoice targets before checking approval and balances (`payables.py:310-396`).

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
- Invoice balance: `balance_due = total − amount_paid`; payment state is UNPAID at zero,
  PAID at `amount_paid >= total`, otherwise PARTIAL (`models.py:1193-1225`).
- Payment values: `gross = Σ requested allocation amounts`; `net = gross − WHT`;
  `unallocated = gross − allocated` (`views/vendor_payments.py:187-211`;
  `models.py:1356-1360`). Example: gross `1,075,000` − WHT `50,000` = bank outflow
  `1,025,000` kobo.
- Explicit allocation is capped by the selected posted invoice's current balance and the
  payment's remaining gross. Automatic service allocation is oldest due date, invoice
  date, then id (`payables.py:458-542`).
- PO summary MTD change is `(current comparable MTD − prior comparable MTD) ÷ prior ×
  100`, one decimal, or `null` when prior is zero (`views/orders.py:158-200`).

## 6. What posting does to the ledger

### Purchase order

Approval creates **no journal**. The PO remains a commitment with priced lines and
workflow evidence (`purchasing.py:140-159`).

### Goods receipt

For each accepted non-stock line, grouped by account:

```text
Dr line expense account (or stock inventory account)   accepted net value
    Cr 2150 GR/IR clearing                              total accepted net value
```

Rejected units do not post. The same transaction posts the journal, advances PO-line
`received_qty`, updates stock movements for stock-backed lines, links the journal, marks
the GRN POSTED, and records audit evidence (`purchasing.py:250-359`). Debits are grouped
only by account id; line descriptions and cost centers are dropped from journal lines
(`purchasing.py:271-321`).

### Vendor invoice

For PO-backed lines:

```text
Dr 2150 GR/IR clearing                invoice net
Dr tax_code.paid_account              recoverable input tax
    Cr vendor.payable_account         invoice gross
```

For non-PO lines, the net debit goes directly to each line expense account rather than
GR/IR. Net and tax debits are grouped only by account id; cost center, PO/GRN line, and
line description do not survive on the journal line. After the balanced journal posts,
PO `invoiced_qty` advances and the bill becomes POSTED (`payables.py:204-275`).

### Vendor payment

```text
Dr vendor.payable_account             gross settled
    Cr payment bank/cash account      gross − WHT
    Cr tax-code WHT account or 2300   WHT retained
```

Posting then recreates the approved draft plan as posted allocations, advances each
invoice's `amount_paid`/payment state, and stores `allocated_amount`; allocation creates
no second journal (`payables.py:391-455,458-551`). Reversal uses finance's reversing
journal and subtracts historical allocations from invoice settlement totals while
retaining the allocation rows as history (`payables.py:554-590`).

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
and journal path are exercised end-to-end in `tests.py:1122-1144,1352-1427`.

## 8. Gotchas / known limitations

- **Wrong ledger for a direct receipt followed by its bill.** A non-PO GRN posts Dr
  expense / Cr GR/IR, but an invoice line linked to that posted GRN and no PO line is
  still treated as a generic non-PO bill and debits expense again. The result is doubled
  expense and stranded GR/IR (`views/receiving.py:305-326`; `payables.py:204-220`).
- **GRN posting is not concurrency-safe.** The atomic worker checks the caller's stale
  GRN object without locking/re-reading it, and it neither locks nor rechecks PO remaining
  quantities. Concurrent post requests can duplicate the journal, while two valid draft
  receipts can together over-receive one PO line (`purchasing.py:250-332`).
- **Non-finite quantities can reach arithmetic.** GRN and invoice line writers use the
  permissive decimal parser without a finite/max-digits guard. Infinity can bypass the
  current comparisons and fail as a 500 during money conversion; invoice NaN/Infinity
  can likewise escape `quantity <= 0` (`views/receiving.py:53-100,287-327`).
- **A PO-backed GRN does not require an approved PO.** Create validates entity/vendor but
  not PO status, and posting does not add the missing gate, so a user with receipt-post
  authority can recognize cost against a DRAFT/PENDING commitment
  (`views/receiving.py:132-155`; `purchasing.py:250-265`).
- **Blocking-match override is parsed and authorized too broadly.** `bool(value)` makes
  JSON strings such as `"false"` truthy, and the same critical invoice-post permission
  both posts normal bills and overrides UNDER_RECEIVED/OVER_BILLED. There is no distinct
  override permission or audit flag (`views/receiving.py:555-572`;
  `management/commands/seed_procurement_permissions.py:46-47`).
- **Departmental attribution is lost.** PO lines carry cost center, but GRN/invoice API
  writers do not copy it to their line models; posting then groups journal debits only by
  account. Departmental actuals cannot be traced back to the commitment's cost center
  (`models.py:994-997,1111-1114,1259-1262`; `views/receiving.py:92-100,319-326`;
  `purchasing.py:271-321`; `payables.py:204-246`).
- **Vendor invoice-number uniqueness is race-prone.** The case-insensitive duplicate
  check exists only in the view, with no database constraint, so concurrent requests can
  create the same vendor reference twice (`views/receiving.py:272-284`;
  `models.py:1185-1191`).
- **Stock-backed receiving is not writable through this API.** The line model and posting
  service support `stock_item`, but GRN create/PATCH never read or set it. This boundary
  is traced and resolved with the inventory slice (`models.py:1093-1097`;
  `views/receiving.py:53-101`; `purchasing.py:267-346`).
- **Price variance is visible but has no variance-account treatment.** A PO price mismatch
  is non-blocking and the invoice clears GR/IR at billed net; any difference from receipt
  value remains in GR/IR rather than posting to a configured purchase-price-variance
  account (`payables.py:62-115,190-220`).
- **Justified by design:** rejected delivery quantity is evidence only and does not
  advance PO received quantity, value inventory, or post to the GL, allowing a later
  replacement delivery (`purchasing.py:254-298,327-346`).
- **Justified by design:** payment allocations are gross AP settlement, not the net bank
  outflow. WHT is part of the invoice settlement even though it is remitted separately
  (`models.py:1285-1291,1363-1368`; `payables.py:391-455`).

## 9. Permissions & tenant isolation

Every view inherits authenticated-user RBAC and resolves the selected ledger entity.
Document reads/writes filter by that entity; vendors, accounts, taxes, bank accounts,
PO/GRN lines, invoices, and allocation targets are re-resolved inside it. Foreign ids
therefore return missing/invalid rather than exposing or mutating another entity
(`views/base.py:55-90,217-231,281-298`; `views/receiving.py:74-90,235-249,295-318`;
`views/vendor_payments.py:45-57,82-109`).

The seeded matrix separates view/create/update/submit/post for each money document.
Goods-receipt post, vendor-invoice post, and every vendor-payment mutation are CRITICAL;
PO submit, receipt create/update, and invoice create/update/submit/match are SENSITIVE
(`management/commands/seed_procurement_permissions.py:29-50`). Payment creation and
posting also recheck active/KYC/hold state and lock vendor/invoice rows at the accounting
boundary (`views/vendor_payments.py:60-71`; `payables.py:310-396`).

There is currently no field-level masking on these serializers. They expose operational
references, totals, account ids/codes, allocations, journal lines, and human-readable
activity, but not raw audit metadata (`serializers.py:740-1130`;
`views/receiving.py:333-396`; `views/vendor_payments.py:122-162`).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py` | PO, GRN, invoice, payment, line/allocation storage and derived totals |
| `views/orders.py` | PO CRUD, filters, summary, contract validation |
| `views/receiving.py` | GRN/invoice CRUD, line validation, matching/post endpoints, detail overlays |
| `views/vendor_payments.py` | Payment plan CRUD, vendor/bank gates, eligible bills, post/cancel/reverse |
| `purchasing.py` | PO creation/pricing/approval and GRN accounting/quantity effects |
| `payables.py` | Invoice pricing/matching/posting and payment posting/allocation/reversal |
| `approvals.py` / `workflow_handlers.py` | Threshold workflows and terminal document effects |
| `serializers.py` | Public P2P response shapes and display-state overlays |
| `constants.py` | Match/payment states and 2150 GR/IR / 2300 WHT control codes |
| `urls.py` | `/v1/procurement/` route map |
| `management/commands/seed_procurement_permissions.py` | RBAC registry/sensitivity/platform grants |

## 11. Test coverage & gaps

The current procurement suite is **209 green**. P2P service/API tests cover GRN whole
quantity/remainder validation, DRAFT edits, update permission, expense→GR/IR posting;
invoice approval, split-line aggregation, GR/IR clearing, input VAT, blocking overbill
and under-receipt, invoice view permission and cross-entity detail; payment WHT split,
approval, plan validation, reversal, mutation permissions, cross-entity detail, draft
plans, partial settlement and held vendors; AP reconciliation; and the full PR-to-payment
chain (`tests.py:815-1427`). Purchase-order console and workflow tests cover response
data, filters/KPIs, permission gates, entity isolation, workflow routing, and terminal
approval effects (`tests.py:4339-4902`).

Missing regression coverage mirrors §8: concurrent GRN post/over-receipt, direct-GRN
invoice clearing, finite/bounded GRN and invoice quantities, approval status required for
PO-backed receipt, strict variance-override parsing/authorization/audit, cost-center
carry-through, database-enforced vendor-reference uniqueness, stock-item API receipt,
and purchase-price-variance accounting. Empty-list envelope assertions exist broadly in
the procurement console suite but are not explicit for every one of the four P2P list
routes.
