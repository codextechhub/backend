# procurement_sourcing

Procurement sourcing covers the **internal request to buy, competitive vendor
invitation, quoted prices, and selection of a winning offer**. Routes are mounted at
`/v1/procurement/`; every route in this slice requires `?entity=<id|code>`.

---

## 1. What it is (and what it is NOT)

- A `PurchaseRequisition` captures internal buying intent and an estimated value before
  any vendor commitment (`models.py:582-630`).
- A `RequestForQuotation` turns a specification into an invitation sent to eligible
  vendors; `RfqInvitation` is the durable addressee list (`models.py:674-745`).
- A `VendorQuotation` is an invited vendor's priced response. Awarding a submitted offer
  rejects the competing submitted offers and creates a **DRAFT purchase order**
  (`sourcing.py:285-404`).

**This does NOT post to the General Ledger.** Requisition approval authorizes intent;
RFQ issue, quotation submission, and award preserve commercial evidence. Even the PO
created by an award remains an unposted commitment. The first possible GL event is a
later goods-receipt posting (`sourcing.py:1-8`; `models.py:876-881`).

## 2. Domain model

| Model | Key fields | Tenant/relationship rules |
|---|---|---|
| `PurchaseRequisition` | title, requester, request/needed dates, cost center, justification, `estimated_total` kobo, shared `status`, workflow `approval_state` | Protected entity; indexed by `(entity, status)` and `(entity, request_date)` (`models.py:582-618`) |
| `PurchaseRequisitionLine` | catalog snapshot, description, quantity `Decimal(14,4)`, unit, estimated unit price kobo, expense/tax defaults | Cascades with requisition; catalog/account/tax are protected (`models.py:633-664`) |
| `RequestForQuotation` | optional requisition, title, `rfq_status`, issue/due dates, optional `budget_estimate` kobo, notes | Protected entity/requisition; indexed by entity/status and entity/issue date (`models.py:674-707`) |
| `RfqInvitation` | RFQ + vendor | Unique `(rfq, vendor)`; RFQ cascades, vendor is protected; response state is derived, not stored (`models.py:713-742`) |
| `RfqLine` | description, quantity `Decimal(14,4)`, optional requisition line, expense account, tax code | Cascades with RFQ; source/account/tax references are protected (`models.py:748-770`) |
| `VendorQuotation` | RFQ, vendor, `quotation_status`, dates, currency, lead time, reference/notes, subtotal/tax/total kobo, awarded PO | Protected entity/RFQ/vendor; awarded PO is nullable `SET_NULL`; indexed by entity/status, RFQ, and vendor (`models.py:776-832`) |
| `VendorQuotationLine` | optional RFQ line, description, expense account, quantity, unit price/net/tax kobo, tax code | Cascades with quotation; RFQ line/account/tax are protected (`models.py:838-866`) |

RFQs and quotations use their own `rfq_status` / `quotation_status` overlays; their
inherited finance-document `status` is unused (`models.py:674-682,776-783`).

## 3. Endpoint map

Request bodies below contain only fields the view reads. List endpoints return the
standard paginated `{pagination, data}` envelope (`views/base.py:281-298`).

| Method + path | permission key | what it does | request body / query fields actually read | response shape |
|---|---|---|---|---|
| `GET /requisitions/` | `procurement.requisition.view` | List/search entity requisitions | Query `status`, `search` | Paginated requisition headers + lines (`views/requisitions.py:89-131`; `serializers.py:447-491`) |
| `POST /requisitions/` | `procurement.requisition.create` | Create a DRAFT requisition and replace-priced estimate lines | `title`, `request_date`, `needed_by?`, `cost_center?`, `justification?`, `lines[]`: `line_no?`, `catalog_item?`, `description?`, `quantity?`, `unit?`, `estimated_unit_price?`, `expense_account?`, `tax_code?` | `201` requisition + lines (`views/requisitions.py:133-152`) |
| `GET /requisitions/summary/` | `procurement.requisition.view` | Entity KPI totals and current/prior partial-month comparisons | — | `{as_of, pending_approval, approved_mtd, draft, total_value_mtd}` (`views/requisitions.py:202-259`) |
| `GET /requisitions/budget-availability/` | `procurement.requisition.view` | Compare annual approved budget with open PO commitments | Query `cost_center` required, `date?` | `{has_budget, period, budget, committed, available}` in kobo (`views/requisitions.py:262-319`) |
| `GET /requisitions/<pk>/` | `procurement.requisition.view` | Read one entity requisition | — | Requisition + lines + `workflow_instance_id` (`views/requisitions.py:164-176`) |
| `PATCH /requisitions/<pk>/` | `procurement.requisition.update` | Edit a DRAFT; `lines` is a full replacement | Header and line fields accepted by POST, all optional | Updated requisition (`views/requisitions.py:178-199`) |
| `POST /requisitions/<pk>/submit/` | `procurement.requisition.submit` | Hand the requisition to `vs_workflow` | — | Workflow id/status, approval state, and refreshed requisition (`views/requisitions.py:322-341`) |
| `GET /rfqs/` | `procurement.rfq.view` | List sourcing events with SQL-derived counts | Query `status`, `q`/`search` | Paginated RFQ rows (`views/orders.py:331-346,428-448`; `serializers.py:551-568`) |
| `POST /rfqs/` | `procurement.rfq.create` | Create a DRAFT specification and optional invitation set | `requisition?`, `title?`, `issue_date`, `response_due_date?`, `budget_estimate?`, `notes?`, `invited_vendors?`; `lines[]`: `line_no?`, `description`, `quantity?`, `requisition_line?`, `expense_account?`, `tax_code?` | `201` full RFQ detail (`views/orders.py:450-484`) |
| `GET /rfqs/summary/` | `procurement.rfq.view` | Count drafts/open events/responses/near deadlines | — | `{draft, open, responses_in, closing_soon}` (`views/orders.py:593-620`) |
| `GET /rfqs/<pk>/` | `procurement.rfq.view` | Read lines, invitations, newest-first quotations, activity | — | Full RFQ detail (`views/orders.py:499-505`; `serializers.py:590-655`) |
| `PATCH /rfqs/<pk>/` | `procurement.rfq.update` | Edit a DRAFT; lines/invitations are full replacements when present | `title?`, `issue_date?`, `response_due_date?`, `budget_estimate?`, `notes?`, `lines?`, `invited_vendors?` | Updated full RFQ (`views/orders.py:507-542`) |
| `POST /rfqs/<pk>/issue/` | `procurement.rfq.issue` | Freeze a DRAFT with at least one line and invitee as ISSUED | — | Full issued RFQ (`views/orders.py:545-557`; `sourcing.py:85-118`) |
| `POST /rfqs/<pk>/close/` | `procurement.rfq.issue` | Close an ISSUED event without award and reject live bids | `reason?` | Full closed RFQ (`views/orders.py:560-575`; `sourcing.py:171-196`) |
| `POST /rfqs/<pk>/cancel/` | `procurement.rfq.issue` | Abandon a non-awarded event and reject live bids | `reason?` | Full cancelled RFQ (`views/orders.py:578-590`; `sourcing.py:146-168`) |
| `GET /quotations/` | `procurement.quotation.view` | List/search offers | Query `status`, `rfq`, `vendor`, `q`/`search` | Paginated quotation rows (`views/orders.py:669-697`; `serializers.py:676-698`) |
| `POST /quotations/` | `procurement.quotation.create` | Create and price an invited eligible vendor's DRAFT offer against an ISSUED RFQ | `rfq`, `vendor`, `quote_date`, `valid_until?`, `currency?`, `lead_time_days?`, `reference?`, `notes?`; `lines[]`: `line_no?`, `rfq_line?`, `description?`, `expense_account?`, `quantity?`, `unit_price`, `tax_code?` | `201` quotation + priced lines/totals/activity (`views/orders.py:699-738`) |
| `GET /quotations/<pk>/` | `procurement.quotation.view` | Read one offer | — | Full quotation detail (`views/orders.py:753-759`; `serializers.py:701-733`) |
| `PATCH /quotations/<pk>/` | `procurement.quotation.update` | Edit/reprice a DRAFT; lines are a full replacement | `quote_date?`, `valid_until?`, `lead_time_days?`, `reference?`, `notes?`, `lines?` | Updated full quotation (`views/orders.py:761-793`) |
| `POST /quotations/<pk>/submit/` | `procurement.quotation.submit` | Make a DRAFT offer firm | — | Submitted full quotation (`views/orders.py:796-810`; `sourcing.py:222-282`) |
| `POST /quotations/<pk>/award/` | `procurement.quotation.award` | Select a submitted offer and atomically create its DRAFT PO | `order_date?` | `201` full purchase order (`views/orders.py:813-835`; `serializers.py:776-812`) |

## 4. Lifecycle / state machine

```text
Requisition status:
DRAFT ─submit─▶ PENDING_APPROVAL ─workflow approve─▶ APPROVED
   ▲                    ├─workflow reject──────────▶ CANCELLED
   └──── withdraw/cancel workflow instance ────────┘

RFQ status:
DRAFT ─issue─▶ ISSUED ─award quote─▶ AWARDED
  └─cancel─▶ CANCELLED  ├─close────▶ CLOSED
                       └─cancel───▶ CANCELLED

Quotation status:
DRAFT ─submit─▶ SUBMITTED ─award─▶ AWARDED
  └──────────── close/cancel RFQ ─▶ REJECTED
                 sibling awarded ─▶ REJECTED
```

Requisition submission sets both workflow `approval_state=PENDING` and shared
`status=PENDING_APPROVAL`; approval changes both to APPROVED, rejection stores
`approval_state=REJECTED` and shared `status=CANCELLED`, and workflow withdrawal resets
the document to DRAFT (`approvals.py:136-172,179-239`). The default workflow always has
a manager stage and adds a senior stage at `estimated_total >= 50,000,000` kobo
(`approvals.py:62-124`; `constants.py:158-164`).

Only DRAFT RFQs and quotations are editable. An RFQ needs a line and invitation before
issue; quotation create/submit requires ISSUED RFQ, an invitation, and a vendor that is
active, not on hold, and not KYC-rejected (`sourcing.py:30-118,222-282`;
`purchasing.py:91-99`). Award locks the RFQ, quotation, and vendor in that order, requires a live
SUBMITTED offer, and performs PO creation plus winner/loser/RFQ state changes in one
transaction (`sourcing.py:285-404`).

## 5. Calculations

- Requisition line estimate: `round(quantity × estimated_unit_price)` kobo; the current
  property uses Decimal's integral rounding, then the header sums all line estimates
  (`models.py:620-630,661-664`). Example: `2.5 × 120,000 = 300,000` kobo.
- Quotation line net: `quantity × unit_price`, rounded `ROUND_HALF_UP` to whole kobo
  (`receivables.py:42-45`; `sourcing.py:203-219`). Example:
  `2.5 × 120,000 = 300,000` kobo.
- Line tax: `round_half_up(net × rate_bps ÷ 10,000)` kobo
  (`receivables.py:49-57`; `sourcing.py:211-219`). At 7.5% (`750` bps),
  `300,000 × 750 ÷ 10,000 = 22,500` kobo.
- Quotation `subtotal = Σ line.net_amount`; `tax_total = Σ line.tax_amount`;
  `total = subtotal + tax_total` (`models.py:822-832`). The example totals
  `300,000 + 22,500 = 322,500` kobo.
- Requisition trend: `((current MTD − comparable prior MTD) ÷ prior MTD) × 100`, rounded
  to one decimal; it returns `null` when the prior amount is zero
  (`views/requisitions.py:206-259`).
- Budget availability: `approved annual budget for cost center − net value of
  PENDING_APPROVAL/APPROVED PO lines for that cost center in the same fiscal year`
  (`views/requisitions.py:282-319`).

## 6. What posting does to the ledger

Nothing in this slice posts, so there are no Dr/Cr lines. Requisition submission and
approval write workflow/document state; sourcing actions write RFQ/quotation state and
finance audit rows (`approvals.py:136-203`; `sourcing.py:85-196,222-404`).

Award creates a DRAFT PO and carries the winning quotation's entity, branch, vendor,
currency, vendor payment terms, reference, and RFQ requisition link. Per line it carries
description, quantity, unit price, tax code, line number, expense account (with
vendor/category fallback), and the source requisition line reached through the RFQ line
(`sourcing.py:331-383`). It drops quotation notes and lead time. The originating
requisition line's cost center now survives; when no source line exists, the RFQ header
requisition's cost center is the fallback (`sourcing.py:354-382`).

## 7. Worked example

An issued RFQ has one requested line and vendor `TECH01` is invited. The vendor submits:

```json
POST /v1/procurement/quotations/?entity=LEKKI
{
  "rfq": 41,
  "vendor": "TECH01",
  "quote_date": "2026-07-22",
  "valid_until": "2026-08-21",
  "currency": "NGN",
  "lead_time_days": 7,
  "reference": "TECH-Q-104",
  "lines": [{
    "rfq_line": 91,
    "quantity": "2.5000",
    "unit_price": 120000,
    "tax_code": "VAT-7.5"
  }]
}
```

The server derives, rather than reads, the money fields:

```json
{
  "quotation_status": "DRAFT",
  "rfq_id": 41,
  "vendor_code": "TECH01",
  "subtotal": 300000,
  "tax_total": 22500,
  "total": 322500,
  "lines": [{
    "rfq_line_id": 91,
    "quantity": "2.5000",
    "unit_price": 120000,
    "net_amount": 300000,
    "tax_amount": 22500
  }]
}
```

After submit, `POST /quotations/<pk>/award/` changes this offer to AWARDED, rejects
other submitted offers, closes the RFQ as AWARDED, and returns a DRAFT PO priced to the
same `322500` kobo. There is still no journal (`views/orders.py:699-738,796-835`;
`sourcing.py:203-219,285-404`).

## 8. Gotchas / known limitations

- ✅ **Awarded sourcing remains visible in departmental commitments.** Award now copies
  the originating requisition line's cost center, falls back to the RFQ header
  requisition's center, and loads that lineage in the quotation-line query rather than
  adding a query per line. Budget availability therefore sees the resulting PO-line
  commitment (`sourcing.py:354-382`; `views/requisitions.py:299-310`).
- ✅ **RFQ header and line lineage agree.** When an RFQ names a requisition, create and
  DRAFT PATCH accept source lines only from that same requisition; a mismatched
  same-entity line returns a field-level 400 and the atomic replacement preserves the
  previous lines. Headerless RFQs retain optional entity-scoped line lineage
  (`views/orders.py:367-399,450-484,507-542`).
- ✅ **Requisition quantities use the strict shared sourcing boundary.** POST and PATCH
  now reject zero, negative, non-finite, and oversized quantities before rewriting any
  line, matching RFQ/quotation behavior (`views/requisitions.py:65-86`;
  `views/base.py:113-134`).
- ✅ **Multiple vendor quotations are retained by design.** The RFQ detail prefetch orders
  them by `created_at DESC, id DESC`; the newest offer is the invitation's canonical
  response and the quotation list uses the same newest-to-oldest cache without N+1
  queries (`views/orders.py:349-364`; `serializers.py:628-652`).
- **Justified by design:** response deadlines are informative, not enforced. Create/submit
  checks that the RFQ is ISSUED but does not reject a quote after `response_due_date`;
  buyers close the RFQ manually when bidding ends (`views/orders.py:699-723`;
  `sourcing.py:222-267`).
- ✅ **Sourcing transitions are serialized.** Issue re-locks/rechecks the RFQ; submit and
  award use the shared `RFQ → quotation → vendor` order; close/cancel lock live bids in
  id order after the RFQ. Concurrent edits and lifecycle actions therefore observe the
  committed authoritative state without reverse lock cycles
  (`sourcing.py:85-196,222-310`).
- **Justified by design:** quotation expiry is a display-only `is_expired` overlay; award
  still enforces `valid_until`, so no scheduler is needed to rewrite historical offer
  status (`serializers.py:519-531`; `sourcing.py:322-329`).

## 9. Permissions & tenant isolation

All views inherit active-user authentication and RBAC from `_ProcBase`, resolve the
selected ledger entity, and scope document `pk` lookups to it (`views/base.py:281-298`).
Cost centers, vendors, accounts, tax codes, requisitions, and RFQ/quotation targets are
entity-scoped; quotation RFQ-line references are additionally scoped to the selected
RFQ (`views/requisitions.py:54-62`; `views/base.py:55-90,217-231`;
`views/orders.py:634-660`). A foreign id therefore behaves as missing/invalid rather
than exposing another entity's row.

The seeded matrix separates requisition view/create/update/submit, RFQ
view/create/update/issue, and quotation view/create/update/submit/award. Submit, issue,
and award are sensitive; ordinary sourcing reads/edits are normal. Close/cancel share
the RFQ `issue` authority (`management/commands/seed_procurement_permissions.py:29-40`).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py` | Requisition, RFQ/invitation, quotation, line storage and totals |
| `views/requisitions.py` | Requisition CRUD, summary, budget availability, workflow hand-off |
| `views/orders.py` | RFQ/quotation CRUD, filters, validation, lifecycle endpoints |
| `sourcing.py` | Invitation, RFQ/quotation lifecycle, pricing, atomic award-to-PO |
| `approvals.py` | Default approval ladder and procurement workflow effects |
| `workflow_handlers.py` | `vs_workflow` registration, summaries, terminal callbacks |
| `purchasing.py` | Vendor eligibility, requisition approval, direct PR-to-PO comparison |
| `serializers.py` | Public requisition/RFQ/quotation shapes and sourcing activity overlays |
| `constants.py` | RFQ, quotation, and procurement approval states/thresholds |
| `urls.py` | `/v1/procurement/` route map |
| `management/commands/seed_procurement_permissions.py` | RBAC registry and platform grants |

## 11. Test coverage & gaps

The current procurement suite is **209 green**. Requisition console coverage verifies
entity-scoped summaries, create/derived totals, foreign cost-center rejection, status
filtering/search, strict quantity validation/rollback, annual budget commitment math,
and endpoint RBAC (`tests.py:1430-1683`). Workflow coverage verifies manager approval,
senior threshold escalation, and rejection-to-cancellation (`tests.py:4685-4902`).

`SourcingTests` and `SourcingConsoleAPITests` cover pricing/lifecycle services, invited
vendor eligibility, issue/close/cancel/award behavior, expired-offer refusal, list and
empty shapes, permissions, cross-entity ids, validation bounds, invitation replacement,
detail derivation, API award conversion, cost-center precedence, and sourced commitment
visibility in the budget endpoint, source-line consistency with PATCH rollback, and
newest-quotation canonical ordering (`tests.py:2468-2792,2950-3475`). Real PostgreSQL
race coverage proves issue waits for a draft edit and rechecks its committed lines, and
only one simultaneous quotation submission succeeds (`tests.py:2793-2949`).

The remaining deadline/expiry behaviors are intentional §8 decisions, not uncovered
fixes. Direct ORM/import writers must still preserve RFQ parent locking and source-line
consistency because those rules are enforced at the service/API boundary.
