# procurement_reports

Procurement reporting is the entity-scoped read side over posted payables,
receipts, invoices, payments, purchase orders, assessments, workflow approvals,
and procurement audit events. It provides a current dashboard, AP aging and
reconciliation, cash requirements, GR/IR control and drill-downs, realised spend,
vendor performance, and procure-to-pay cycle time. Routes are mounted at
`/v1/procurement/`; every route requires `?entity=<id|code>`.

---

## 1. What it is (and what it is NOT)

These reports turn already-recorded procurement and finance evidence into
read-only management views. AP reports explain open vendor balances; GR/IR
reports compare posted receipts with the invoice basis that clears account 2150;
analytics aggregate posted documents into spend, supplier, and elapsed-time
measures; the dashboard composes a live operational snapshot
(`reports.py:1-18,71-181,231-397,872-1251`; `dashboard.py:376-456`).

**This is not a historical reporting warehouse, a posting engine, or an editable
report builder.** The report APIs create no journals and accept no stored report
definition. In particular, `as_of` is usually an aging/reference clock rather
than a transaction-date cutoff; only the spend, vendor-performance, and cycle-time
endpoints apply explicit inclusive date windows (`reports.py:71-80,231-240,
604-615,872-905,991-1007,1162-1178`).

The stock reorder and valuation reports share this route family but are documented
with the perpetual inventory ledger in
[`procurement_inventory.md`](procurement_inventory.md).

## 2. Domain model

There is no persisted `ProcurementReport` model. Report rows are dataclasses or
dictionaries assembled at request time from the operational models below. All
money is integer **kobo** internally and API money is rendered as
`{kobo, naira}` (`reports.py:8-18`; `views/base.py:289-292`).

| Source | Report use | Scope and state rules |
|---|---|---|
| `Vendor`, `VendorInvoice`, `VendorPayment`, `VendorPaymentAllocation` | AP aging/reconciliation, cash needs, spend, vendor performance, dashboard overdue invoices | Queries start with `entity`; AP and spend include only `POSTED` invoices, AP excludes fully paid bills, and unallocated posted payments reduce AP net (`reports.py:71-138,153-181,231-276,872-953`; `models.py:123-224,1132-1424`) |
| `PurchaseOrder`, `PurchaseOrderLine`, `GoodsReceivedNote`, `GoodsReceivedNoteLine`, `VendorInvoiceLine` | GR/IR, delivery performance, PO status, cycle time | GR/IR detail uses posted receipts/invoices; line reports exclude cancelled/reversed POs; dashboard status excludes cancelled/reversed POs (`reports.py:284-397,499-805`; `dashboard.py:87-151`) |
| `VendorAssessment` | Latest qualitative supplier scorecard on each vendor-performance row | Immutable entity/vendor snapshot; newest `assessment_date`, then newest id, is authoritative. Scores are 0–100 and weighted into a computed whole-number grade (`models.py:1427-1484`; `reports.py:1118-1132`) |
| `WorkflowInstance`, `WorkflowStageInstance`, `WorkflowStageApprover` | Approvals awaiting the requesting user | Only active, current-attempt, actor-assigned items whose target document resolves to the selected entity are returned (`dashboard.py:209-347`) |
| `FinanceAuditLog` | Five newest dashboard activities | Only successful actions in the explicit procurement allow-list are eligible (`dashboard.py:46-79,348-374`) |

The reports deliberately reuse posted-document snapshots rather than recalculating
today's catalogue prices. GR/IR invoice value uses the linked posted GRN-line price
first, then the PO-line price; a direct invoice line clears no GR/IR
(`reports.py:284-300`).

## 3. Endpoint map

All endpoints are `GET`, require `procurement.report.view`, and require
`?entity=<id|code>`. Query fields below are only those actually read by the view.
Except for the two stock reports documented separately, these responses are not
paginated (`urls.py:113-130`; `views/reports.py:30-502`).

| Method + path | query fields actually read | what it does | response shape |
|---|---|---|---|
| `GET /reports/dashboard/` | — | Live requester-aware dashboard | Entity/currency/as-of/month start; KPIs; category spend; PO statuses; eight-month trend; five activities; up to four assigned approvals (`views/reports.py:358-370`; `dashboard.py:376-456`) |
| `GET /reports/ap-aging/` | `as_of?` | Age open AP by vendor | Buckets, vendor rows with payment terms/gross buckets/outstanding/unallocated credit/net, bucket totals, total net (`views/reports.py:30-62`) |
| `GET /reports/ap-aging/vendor/` | `vendor` id or exact code, `as_of?` | Open-bill evidence for one vendor | Vendor, bucket amounts, outstanding, unallocated credit, net, and invoice rows (`views/reports.py:176-215`) |
| `GET /reports/ap-reconciliation/` | `as_of?` | Compare AP subledger with vendor-linked GL controls | Subledger total, control total, signed difference, reconciled flag (`views/reports.py:65-87`) |
| `GET /reports/ap-cash-requirements/` | `as_of?` | Forecast gross open bills by due window | Forecast buckets, vendor rows, bucket totals, total due (`views/reports.py:110-137`) |
| `GET /reports/grir/` | — | Read account 2150 normal-balance signed net | GR/IR balance and `is_clear` (`views/reports.py:90-107`) |
| `GET /reports/grir-aging/` | `as_of?` | Age signed receipt-grain GR/IR positions | GRN rows, signed bucket totals/open total, GL control, reconciliation difference (`views/reports.py:140-173`) |
| `GET /reports/grir-aging/grn/` | numeric `grn`, `as_of?` | Explain one GRN with PO and matched invoices | Vendor/PO/date/bucket, received/invoiced/open values, distinct linked invoice evidence (`views/reports.py:218-258`) |
| `GET /reports/grir-lines/` | `as_of?` | Compare ordered, received, and invoiced quantities/value by PO line | Entity/as-of and activity-bearing PO-line rows with derived status (`views/reports.py:261-296`) |
| `GET /reports/grir-lines/detail/` | numeric `po_line`, `as_of?` | Explain one PO line | Quantities, values, status/unit price, and posted GRN/invoice evidence (`views/reports.py:299-351`) |
| `GET /reports/spend-analysis/` | `start_date?`, `end_date?`, `category?` code or `UNCATEGORISED` | Aggregate posted-invoice spend | Vendor/category/month rows plus net/tax/gross totals and invoice count (`views/reports.py:373-421`) |
| `GET /reports/vendor-performance/` | `start_date?`, `end_date?` | Blend ordering, receipt, invoice, payment, and scorecard evidence | One row per activity-bearing vendor in the evidence set, sorted by billed total (`views/reports.py:423-471`) |
| `GET /reports/cycle-time/` | `start_date?`, `end_date?` | Average the four P2P hops on settled invoice chains | Four named stages with samples/averages plus end-to-end average/count (`views/reports.py:474-502`) |

The report API does not currently expose the services' optional `vendor` filter
for spend or vendor performance. Vendor detail uses a separate entity-scoped
drawer endpoint that calls those services with a resolved vendor
(`reports.py:872-905,991-1036`; `views/vendors.py:641-689`).

## 4. Lifecycle / state machine

Reports have no mutable lifecycle. Their eligibility rules follow source-document
state:

```text
invoice POSTED and not PAID ─────────────▶ AP aging / cash requirements
posted unallocated payment ──────────────▶ AP net credit
GRN POSTED ──────────────────────────────▶ GR/IR received side
invoice POSTED and linked to PO/GRN line ▶ GR/IR invoiced side
PO not CANCELLED/REVERSED ───────────────▶ performance / GR/IR PO-line report
payment POSTED with allocation ──────────▶ performance / cycle-time sample
assessment recorded ────────────────────▶ newest scorecard on vendor performance
successful allow-listed audit event ────▶ dashboard recent activity
```

Posting, reversal, allocation, and approval transitions are owned by the P2P
services and documented in
[`procurement_p2p_chain.md`](procurement_p2p_chain.md). Reports simply re-query
the resulting state (`reports.py:100-137,247-276,349-397,1028-1137,
1190-1251`).

## 5. Calculations

### AP aging, reconciliation, and cash requirements

- `days_overdue = as_of − (due_date or invoice_date)`. Buckets are `current`
  for `≤0`, `1-30`, `31-60`, `61-90`, and `90+` beginning on day 91. Example:
  a 10 January bill aged on 15 February is 36 days overdue and lands in `31-60`
  (`reports.py:29-43,100-115`).
- `balance_due = invoice.total − amount_paid`; only positive balances contribute.
  `vendor_net = Σ balance_due − Σ posted payment.unallocated_amount`.
  Example: `1,000,000` open and `250,000` prepaid gives `750,000` kobo net;
  credits do not reduce a particular aging bucket (`models.py:1203-1206`;
  `reports.py:100-137`).
- `AP difference = total vendor net − Σ normal-balance GL net of each distinct
  vendor payable account`; reconciled means exactly zero. Shared controls are
  de-duplicated. Example: `750,000 − 750,000 = 0` (`reports.py:153-181`).
- Cash forecast uses `days_until_due = (due_date or invoice_date) − as_of` and
  buckets `<0 overdue`, `0-7`, `8-30`, `31-60`, `61-90`, `90+`. It is gross:
  unallocated vendor payments are not netted. A bill due in five days contributes
  its full open kobo balance to `0-7` (`reports.py:192-208,231-276`).

### GR/IR

- Invoice clearing basis is
  `round_half_up(invoice_quantity × linked posted GRN unit price)`; PO unit price
  is the fallback, while a direct invoice contributes zero. Example: 10 units
  billed at `120,000` against a receipt snapshot of `100,000` clear `1,000,000`
  kobo from GR/IR; the `200,000` price difference is PPV, not residual GR/IR
  (`reports.py:284-300`; `purchasing.py:747-801`).
- Per receipt, `open_value = GRN.total_value − Σ linked invoice clearing basis`.
  Values are signed: positive is received-not-invoiced; negative is invoice-heavy.
  Zero rows are omitted (`reports.py:330-397`).
- Per PO line, received quantity/value sum posted GRN lines; invoiced
  quantity/value sum posted invoice lines on the PO line. `grir_balance =
  received_value − invoiced_value`. Quantity chooses the status first; equal
  quantities use the value sign, and only equal quantity plus zero value is
  `Cleared` (`reports.py:559-576,604-683`).
- Account 2150 is reported on its normal credit-balance sign: positive is
  received-not-invoiced, negative is invoice-first/over-cleared, zero is clear
  (`reports.py:786-805`).

### Spend, vendor performance, and cycle time

- Spend is the sum of `subtotal`, `tax_total`, and `total` from posted invoices
  whose `invoice_date` is inside the inclusive window. It groups the same pass by
  vendor, vendor category, and `YYYY-MM`; vendor/category rows sort by descending
  gross, months chronologically (`reports.py:872-953`).
- `on_time_rate = on_time_receipts ÷ (on_time_receipts + late_receipts)`, rounded
  to four decimals; unrated receipts with no PO expected date stay out of the
  denominator. Example: 3 on-time and 1 late is `0.75`
  (`reports.py:976-980,1042-1061`).
- `avg_payment_days` is the one-decimal mean of
  `payment_date − invoice_date` per allocation row; payment count de-duplicates
  payment documents, but an invoice paid in instalments contributes multiple
  duration samples. Example: 10 and 20 days average `15.0`
  (`reports.py:819-821,1080-1108`).
- Cycle time averages, to one decimal, requisition→PO, PO→earliest posted receipt,
  earliest receipt→invoice, and invoice→payment. The inclusive filter is on
  payment date. Each invoice is sampled once, and end-to-end requires a
  requisition, PO, posted receipt, invoice, and allocating posted payment
  (`reports.py:1162-1251`).
- Dashboard MTD delta is `(current − prior) ÷ prior × 100`, rounded to one decimal;
  it is `null` when prior is zero. The comparison uses the same elapsed days in
  the previous month. Monthly trend is eight calendar months, including zero
  months; the current month stops at today (`dashboard.py:80-86,153-207,376-402`).
- Dashboard category spend shows the largest five current-MTD categories and
  combines the rest into an exact `Other` total. Overdue means `due_date < today`,
  and amount is `total − amount_paid` (`dashboard.py:153-179,404-438`).

## 6. What posting does to the ledger

**These GET endpoints post nothing.** They create no journal, mutate no document,
and write no audit record. They read the journal and source-document effects
created by the operational services (`views/reports.py:30-502`).

The ledger balances they explain are:

```text
Vendor invoice:  Cr vendor.payable_account
Vendor payment:  Dr vendor.payable_account

Posted GRN:       Cr 2150 GR/IR clearing
Matched invoice: Dr 2150 GR/IR clearing at receipt/PO basis
Price variance:  Dr/Cr 5160 Purchase Price Variance
```

AP reconciliation reads every distinct payable control referenced by an entity
vendor. GR/IR reads code 2150 directly. Report grouping cannot restore dimensions
or references dropped by posting; journal detail and the P2P report remain the
authoritative posting trace (`reports.py:153-181,786-805`;
`purchasing.py:319-400,747-801`;
[`procurement_p2p_chain.md`](procurement_p2p_chain.md)).

## 7. Worked example

Assume ACME has one posted `1,000,000` kobo invoice due 10 January, a posted
unallocated payment of `250,000`, and a matching AP GL net of `750,000`. On
15 February:

```http
GET /v1/procurement/reports/ap-aging/?entity=LEKKI&as_of=2026-02-15
```

The bill is 36 days overdue, so the gross invoice stays in `31-60`; the prepayment
reduces only the vendor/report net:

```json
{
  "entity": "LEKKI",
  "as_of": "2026-02-15",
  "buckets": ["current", "1-30", "31-60", "61-90", "90+"],
  "rows": [{
    "vendor_id": 12,
    "code": "ACME",
    "name": "ACME Supplies",
    "payment_terms": "NET_30",
    "buckets": {
      "current": {"kobo": 0, "naira": "0.00"},
      "1-30": {"kobo": 0, "naira": "0.00"},
      "31-60": {"kobo": 1000000, "naira": "10,000.00"},
      "61-90": {"kobo": 0, "naira": "0.00"},
      "90+": {"kobo": 0, "naira": "0.00"}
    },
    "outstanding": {"kobo": 1000000, "naira": "10,000.00"},
    "unallocated_credit": {"kobo": 250000, "naira": "2,500.00"},
    "net": {"kobo": 750000, "naira": "7,500.00"}
  }],
  "bucket_totals": {
    "current": {"kobo": 0, "naira": "0.00"},
    "1-30": {"kobo": 0, "naira": "0.00"},
    "31-60": {"kobo": 1000000, "naira": "10,000.00"},
    "61-90": {"kobo": 0, "naira": "0.00"},
    "90+": {"kobo": 0, "naira": "0.00"}
  },
  "total_net": {"kobo": 750000, "naira": "7,500.00"}
}
```

The reconciliation endpoint then returns subledger `750,000`, control `750,000`,
difference `0`, and `is_reconciled: true`. No journal is generated by either
request (`reports.py:71-181`; `views/reports.py:30-87`).

## 8. Gotchas / known limitations

### Fix automatically

- **Signed GR/IR reconciliation is wrong for a net debit.** Receipt rows and the
  account control both preserve direction, but aging currently compares
  `total_open` with `abs(control_balance)`. A valid `-500,000` row against a
  `-500,000` control falsely reports a `-1,000,000` difference. The comparison
  must remain signed and needs invoice-heavy regression coverage
  (`reports.py:319-327,372-397,786-805`).

### Recommend fixing

- **The main analytical lists are unbounded.** AP aging, cash requirements,
  GR/IR aging/lines, spend dimensions, vendor performance, and drill-down invoice
  arrays serialize every matching row. This will increase response size and Python
  memory with transaction history. Add the standard pagination envelope without
  changing whole-report totals (`views/reports.py:30-351,373-502`).
- **`as_of` sounds historical but does not cut off later documents or GL entries.**
  AP, cash, and GR/IR queries include every currently posted source row and use
  `as_of` only to choose a bucket; AP reconciliation also reads the current GL.
  Either implement true effective-date snapshots consistently or rename/document
  the input as an aging reference date in the public contract
  (`reports.py:71-80,153-181,231-240,330-397,604-615`).
- **Invalid analytical filters can look like valid empty reports.** The views parse
  dates but do not reject `start_date > end_date`; an unknown category code simply
  returns zero spend. Validate ranges and category membership so input mistakes
  return 400 instead of plausible-looking zeros (`views/reports.py:373-485`;
  `reports.py:895-905`).

### Judgment calls

- **GRN aging attributes only invoices linked to a GRN line.** A posted invoice
  linked only to a PO line debits GR/IR but cannot be assigned to a particular
  receipt, so it appears as a control difference even though the PO-line report
  can explain it. Keeping this strict evidence rule is defensible; automatic
  allocation across receipts would need an explicit FIFO or matching policy
  (`reports.py:330-397,604-683`).
- **Cash requirements is gross and AP aging is net.** Unallocated prepayments
  reduce AP net but not invoice cash buckets. This avoids pretending that an
  unallocated payment belongs to a particular due bill, but treasury may prefer
  a separate “available vendor credits” figure (`reports.py:117-137,231-276`).
- **Cycle-time samples can be negative and instalments use one payment.** Backdated
  documents remain visible as negative durations; an invoice allocated by several
  payments is sampled once using whichever in-window allocation is encountered
  first. That exposes source-data anomalies but is not a weighted time-to-full-
  settlement measure (`reports.py:1162-1251`).

### Justified by design

- **All report routes share `procurement.report.view`.** The key is seeded as a
  normal read permission; every query still resolves an entity, and detail ids are
  re-qualified through that entity. Foreign vendor references fail resolution and
  foreign GRN/PO-line ids return an indistinguishable 404
  (`management/commands/seed_procurement_permissions.py:38-45`;
  `views/reports.py:30-502`).
- **Vendor performance keeps computed and assessed evidence separate.** On-time
  rate comes only from dated receipts versus PO expectations. The newest immutable
  assessment supplies quality, invoice-accuracy, responsiveness, overall score,
  and grade but never overwrites that computed rate
  (`reports.py:1042-1061,1118-1132`; `views/reports.py:423-471`).
- **The dashboard is deliberately live and presentation-capped.** The endpoint
  accepts no client `as_of`; it uses the server's local date. Activity is limited
  to five, approval cards to four, while the pending KPI retains the full
  requester-eligible count (`views/reports.py:358-370`;
  `dashboard.py:348-456`).

## 9. Permissions & tenant isolation

Every endpoint in §3 declares `procurement.report.view`; the seed command creates
that normal read permission (`views/reports.py:30-502`;
`management/commands/seed_procurement_permissions.py:38-45`).

`resolve_entity(request)` supplies the common tenant/entity boundary before any
service runs. Services then start from `entity` or an entity-owned parent. Vendor
drawer references resolve inside the selected entity; numeric GRN and PO-line
details include the entity in their lookup and return 404 for foreign ids
(`views/reports.py:38-42,188-192,230-237,312-319,366-386`;
`reports.py:98-105,499-519,720-728`).

The dashboard adds actor scope: workflow approval cards require the requesting
user to be an active approver on the current attempt, and their target document
must belong to the selected entity (`dashboard.py:209-347`). No endpoint exposes
raw audit metadata; recent activity emits an allow-listed label, reference, actor,
and timestamp, not the raw audit message (`dashboard.py:348-374`).

There is no report-specific field-level masking. A user granted report access can
see vendor financial totals, operational document references, and the selected
assessment metrics described in §3. The vendor-performance response omits
assessment notes and assessor identity (`views/reports.py:423-471`).

## 10. Code map

| File | Responsibility |
|---|---|
| `apps/vs_procurement/urls.py:113-130` | Public report route map |
| `apps/vs_procurement/views/reports.py:30-502` | Entity resolution, permission keys, query parsing, and API shapes |
| `apps/vs_procurement/reports.py:25-805` | AP, cash, GR/IR, and drill-down calculations |
| `apps/vs_procurement/reports.py:819-1251` | Spend, vendor performance, and cycle-time calculations |
| `apps/vs_procurement/dashboard.py:46-456` | Dashboard KPIs, workflow cards, activity, trend, and presentation caps |
| `apps/vs_procurement/models.py:1427-1484` | Immutable vendor assessment and computed score/grade |
| `apps/vs_procurement/views/assessments.py:22-111` | Scorecard validation, list/create permissions, and serialized assessment evidence |
| `apps/vs_procurement/management/commands/seed_procurement_permissions.py:38-45` | Report and assessment permission seeds |

## 11. Test coverage & gaps

Existing tests cover:

- report permission denial, AP/spend/performance entity isolation, malformed
  `as_of` regression, and empty AP/spend/performance arrays
  (`tests.py:2615-2774`);
- spend vendor/category/month grouping, chronological periods, date filtering,
  vendor delivery/payment metrics, and all four cycle-time hops
  (`tests.py:2509-2613,2724-2760`);
- newest vendor assessment selection and serialized separation from computed
  on-time performance (`tests.py:2880-2950`);
- AP reconciliation through invoice/payment, cash forecast bucketing, positive
  GR/IR aging, matched clearing, PPV-neutral GR/IR reports, PO-line status/detail,
  and foreign-detail isolation (`tests.py:2178-2200,2990-3091,3179-3351,
  5151-5226`);
- dashboard entity isolation, overdue balance, PO status, activity caps,
  actor-scoped approvals, vendor-payment workflows, and permission denial
  (`tests.py:5352-5580`).

Before shipping the remaining recommendations, add:

- signed invoice-heavy GR/IR aging/control reconciliation;
- 403 and cross-entity tests for every report route, not only representative
  endpoints;
- API tests for AP reconciliation, cash forecast, GR/IR aging, cycle-time, invalid
  dates/categories, and each empty response shape;
- pagination boundary, stable ordering, whole-report total, and maximum-page-size
  tests if report pagination is selected;
- query-count or scale tests for the largest analytical endpoints;
- explicit coverage for future-dated rows under a past `as_of`, PO-only invoice
  attribution, gross cash versus vendor credits, negative cycle durations, and
  multi-instalment cycle-time policy.
