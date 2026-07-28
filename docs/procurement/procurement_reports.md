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

**This is not a stored reporting warehouse, a posting engine, or an editable
report builder.** The APIs create no journals and persist no report snapshot.
When `as_of` is supplied, AP and GR/IR reconstruct an effective-date view from
posted journal dates and allocations; analytics use their documented inclusive
date windows (`reports.py:71-152,155-262,316-375,402-584,790-1036,
1103-1184,1222-1370,1397-1532`).

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
Large primary lists and invoice-evidence drawers use the standard top-level
`{pagination,data}` envelope: default page 25, `page_size` maximum 100. Report
totals and chart summaries remain whole-entity/window totals, not page subtotals
(`urls.py:113-130`; `views/reports.py:28-43,49-538`).

| Method + path | query fields actually read | what it does | response shape |
|---|---|---|---|
| `GET /reports/dashboard/` | — | Live requester-aware dashboard | Entity/currency/as-of/month start; KPIs; category spend; PO statuses; eight-month trend; five activities; up to four assigned approvals (`views/reports.py:358-370`; `dashboard.py:376-456`) |
| `GET /reports/ap-aging/` | `as_of?`, `page?`, `page_size?` | Age open AP by vendor | Paginated vendor rows with payment terms/gross buckets/outstanding/unallocated credit/net; whole-report bucket totals and total net (`views/reports.py:49-79`) |
| `GET /reports/ap-aging/vendor/` | `vendor` id or exact code, `as_of?`, `page?`, `page_size?` | Open-bill evidence for one vendor | Paginated invoices; whole-vendor bucket amounts, outstanding, unallocated credit, and net (`views/reports.py:195-232`) |
| `GET /reports/ap-reconciliation/` | `as_of?` | Compare AP subledger with vendor-linked GL controls | Subledger total, control total, signed difference, reconciled flag (`views/reports.py:65-87`) |
| `GET /reports/ap-cash-requirements/` | `as_of?`, `page?`, `page_size?` | Forecast gross open bills and available vendor credits | Paginated vendor rows; whole-report gross buckets/total, unallocated credits, and net cash requirement (`views/reports.py:129-158`) |
| `GET /reports/grir/` | `as_of?` | Read account 2150 normal-balance signed net | Effective-date GR/IR balance, echoed as-of, and `is_clear` (`views/reports.py:107-126`) |
| `GET /reports/grir-aging/` | `as_of?`, `page?`, `page_size?` | Age signed receipt-grain GR/IR positions | Paginated GRN rows; whole-report signed buckets/open total, GL control, and difference (`views/reports.py:161-192`) |
| `GET /reports/grir-aging/grn/` | numeric `grn`, `as_of?`, `page?`, `page_size?` | Explain one GRN with PO and attributed invoices | Paginated invoice evidence; whole-GRN vendor/PO/date/bucket and received/invoiced/open values (`views/reports.py:235-273`) |
| `GET /reports/grir-lines/` | `as_of?`, `page?`, `page_size?` | Compare ordered, received, and invoiced quantities/value by PO line | Paginated activity-bearing PO-line rows with derived status (`views/reports.py:276-309`) |
| `GET /reports/grir-lines/detail/` | numeric `po_line`, `as_of?`, `page?`, `page_size?` | Explain one PO line | Paginated invoice evidence; complete posted GRN evidence and whole-line quantities/values/status (`views/reports.py:312-362`) |
| `GET /reports/spend-analysis/` | `start_date?`, `end_date?`, `category?` code or `UNCATEGORISED`, `page?`, `page_size?` | Aggregate posted-invoice spend | Paginated vendor ranking; complete category/month summaries and whole-report net/tax/gross/count (`views/reports.py:384-452`) |
| `GET /reports/vendor-performance/` | `start_date?`, `end_date?`, `page?`, `page_size?` | Blend ordering, receipt, invoice, payment, and scorecard evidence | Paginated activity-bearing vendors sorted by billed total (`views/reports.py:455-504`) |
| `GET /reports/cycle-time/` | `start_date?`, `end_date?` | Average the four P2P hops on fully settled invoice chains | Four stages with valid/excluded samples and averages; end-to-end valid/excluded counts (`views/reports.py:507-538`) |

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
invoice cumulatively fully settled ─────▶ cycle-time sample at final payment date
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
- For an explicit `as_of`, historical paid amount is the sum of allocations from
  payments whose posted journal was effective by that date; a later reversal does
  not rewrite the earlier snapshot. `balance_due_as_of = invoice.total −
  effective allocations`; only positive balances contribute
  (`reports.py:71-152,155-216`).
- `vendor_net = Σ historical balance_due − Σ historical unallocated payment`.
  Example: `1,000,000` open and `250,000` prepaid gives `750,000` kobo net;
  credits do not reduce a particular aging bucket (`reports.py:183-215`).
- `AP difference = total vendor net − Σ normal-balance GL net of each distinct
  vendor payable account through as-of`; reconciled means exactly zero. Shared
  controls are de-duplicated. Example: `750,000 − 750,000 = 0`
  (`reports.py:231-262`).
- Cash forecast uses `days_until_due = (due_date or invoice_date) − as_of` and
  buckets `<0 overdue`, `0-7`, `8-30`, `31-60`, `61-90`, `90+`. Invoice buckets
  remain gross, while `net_cash_requirement = total_due − total historical
  unallocated credit`. A credit-only vendor remains visible
  (`reports.py:273-375`).

### GR/IR

- Invoice clearing basis is
  `round_half_up(invoice_quantity × linked posted GRN unit price)`; PO unit price
  is the fallback, while a direct invoice contributes zero. Example: 10 units
  billed at `120,000` against a receipt snapshot of `100,000` clear `1,000,000`
  kobo from GR/IR; the `200,000` price difference is PPV, not residual GR/IR
  (`reports.py:284-300`; `purchasing.py:747-801`).
- Explicit GRN-line invoice links clear that receipt first. PO-only invoice
  clearing then consumes remaining receipt-line value FIFO by received date,
  GRN id, and line id; one invoice may split exactly across receipts. Excess with
  no receipt capacity remains in the GL control difference rather than creating
  fictitious receipt evidence (`reports.py:402-504`).
- Per receipt, `open_value = GRN.total_value − Σ explicit/FIFO-attributed clearing`.
  Values are signed: positive is received-not-invoiced; negative is invoice-heavy.
  Zero rows are omitted (`reports.py:534-584`).
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
- Cycle time accumulates posted allocations by payment date/id and samples an
  invoice once, on the payment that fully settles its gross. Partial invoices are
  omitted; the inclusive window is anchored on full-settlement date. Negative
  hops are excluded from averages and counted; end-to-end additionally requires
  the complete monotonic sequence request≤PO≤receipt≤invoice≤settlement
  (`reports.py:1397-1532`).
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
reduces only the vendor/report net. The real response now wraps that nested report
in standard pagination:

```json
{
  "success": true,
  "message": "AP aging retrieved.",
  "pagination": {
    "currentPage": 1,
    "pageSize": 25,
    "totalItems": 1,
    "totalPages": 1,
    "next": null,
    "previous": null
  },
  "data": {
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
}
```

The reconciliation endpoint then returns subledger `750,000`, control `750,000`,
difference `0`, and `is_reconciled: true`. No journal is generated by either
request (`reports.py:71-181`; `views/reports.py:30-87`).

## 8. Gotchas / known limitations

### Fixed automatically

- ✅ **Signed GR/IR reconciliation now works for a net debit.** Receipt rows and
  the account control both preserve direction, and aging now calculates
  `difference = total_open − control_balance` without discarding either sign.
  A linked `-500,000` invoice-heavy row against a `-500,000` control therefore
  reconciles to zero; positive receipt-heavy behavior is unchanged and both
  directions have regression coverage
  (`reports.py:319-327,372-397,786-805`).

### Selected follow-ups — fixed

- ✅ **Large report rows and invoice drawers are paginated.** Six primary
  analytical rankings and three invoice-evidence drawers use the standard
  25/default, 100/maximum envelope. Stable ordering is applied before slicing;
  whole-report totals/category/month summaries remain unchanged across pages
  (`views/reports.py:28-43,49-79,129-192,195-362,384-504`).
- ✅ **`as_of` is now a real accounting cutoff.** AP reconstructs invoice balance
  and vendor credit from effective posted allocations, AP/GRIR controls sum posted
  journal lines through the date, and later receipts/invoices/payments do not
  rewrite an earlier snapshot. Payment reversal dates are respected. Legacy
  posted documents without a linked journal cannot enter a historical snapshot,
  and current vendor→control assignments are used because assignment history is
  not modelled (`reports.py:71-152,155-262,316-375,402-584,790-1036`).
- ✅ **Analytical filter mistakes fail loudly.** Spend, performance, and cycle time
  reject inverted ranges; spend resolves an entity category case-insensitively,
  echoes its canonical code, accepts `UNCATEGORISED`, and rejects unknown/foreign
  codes (`views/reports.py:28-33,384-518`).
- ✅ **PO-only GR/IR clearing uses deterministic FIFO evidence.** Explicit
  GRN-line links reserve their receipt first; remaining PO-only clearing splits
  across posted receipt lines by date/id without double counting. Unattributable
  invoice-first excess remains a visible control difference
  (`reports.py:402-504,534-736`).
- ✅ **Cash requirements shows gross obligations and available credits.** Buckets
  stay gross because a prepayment has no invoice due date, while each vendor and
  the report expose unallocated credit and the resulting signed net requirement
  (`reports.py:292-375`; `views/reports.py:129-158`).
- ✅ **Cycle time now measures full settlement and quarantines bad dates.**
  Instalments accumulate chronologically until the invoice is fully settled;
  partial invoices do not enter. Negative hops are counted but excluded, and
  end-to-end requires a fully monotonic source chain
  (`reports.py:1373-1532`; `views/reports.py:507-538`).

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
| `apps/vs_procurement/views/reports.py:28-538` | Entity resolution, permissions, validation, pagination, and API shapes |
| `apps/vs_procurement/reports.py:25-1036` | Historical AP/cash/GRIR calculations, FIFO attribution, and drill-downs |
| `apps/vs_procurement/reports.py:1050-1532` | Spend, vendor performance, and full-settlement cycle-time calculations |
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
  and negative signed GR/IR aging, matched clearing, PPV-neutral GR/IR reports,
  PO-line status/detail, and foreign-detail isolation
  (`tests.py:2178-2200,2990-3091,3179-3351,5151-5260`);
- dashboard entity isolation, overdue balance, PO status, activity caps,
  actor-scoped approvals, vendor-payment workflows, and permission denial
  (`tests.py:5352-5580`).
- report pagination/empty-list/whole-total behavior, historical AP with payment
  reversal and GL reconciliation, historical GR/IR, date/category validation,
  FIFO split/precedence/excess, credit-only cash forecasts, full-settlement
  instalments, and negative/monotonic cycle anomalies
  (`tests.py:6993-7355`).

Remaining lower-priority gaps are exhaustive 403/cross-entity checks for every
route (representative routes and all id-based drawers are covered), formal
query-count ceilings for every aggregate, legacy journal-less historical data,
and temporal vendor→control-account assignment. Independent full-module QA after
this hardening is **263/263 green**.
