# Frontend brief: backend changes needing UI work

**Repo:** `codextechhub/backend`, branch `main`.
**Commits covered:** `e490690`, `b6da683`, `681456f`, `b64a4fc`.
**Docs:** MRD v2.13; module FRDs M07, M17, M18, M19, M20, M21, M22, M23, M24.

Money is **integer kobo** everywhere in requests and responses; convert once at the
edge of your app. Quantities are decimal strings with 4 places. Every list endpoint is
paginated. `success_response` coerces an empty list to `{}`, so code your empty states
against that, not against `[]`.

The nine items below are ordered by urgency. **Items 1, 2 and 3 can break screens that
work today.** Items 4 and 5 leave a new school looking broken. Items 6 to 9 are gaps
and small additions.

---

## 1. Concessions and credit notes now need approval

**Was:** a permission holder could post a concession or a credit note straight to the
ledger, alone. There was no gate even in principle.
**Now:** both have a workflow document type, a handler, a submit endpoint and a submit
permission, and **the post endpoint refuses while the gate is on**.

### What breaks
Any screen with a straight "Post" button on a concession or credit note. Posting a
gated document returns **400** with:

```json
{"detail": "This concession is approval-gated; submit it for approval instead of posting directly."}
```
```json
{"detail": "This note is approval-gated; submit it for approval instead of posting directly."}
```

### New endpoints
| Method | Path | Permission |
|---|---|---|
| POST | `/v1/finance/concessions/<id>/submit/` | `finance.concession.submit` |
| POST | `/v1/finance/credit-notes/<id>/submit/` | `finance.creditnote.submit` |

Both return the document plus an `approval` block:

```json
{
  "approval": {
    "instance_id": "…",
    "parked": true,
    "stage_code": "adjustment_approval",
    "stage_label": "Adjustment approval",
    "approver_source": "ROLE",
    "role_key": "finance-adjustment-approver",
    "requirement": "Appoint somebody to the Finance adjustment approver role.",
    "document_type": "finance.concession"
  }
}
```

`parked: false` means it went into a queue normally. `parked: true` means **nobody can
approve what was just submitted** and the document is stuck. Show `requirement`
verbatim: it is a ready-made sentence written for this purpose. Show `role_key` only
when `approver_source` is `"ROLE"`; it is blank otherwise.

### The threshold, and why one screen behaves two ways
Concessions and credit notes are gated **at or above ₦50,000** (`WF_ADJUSTMENT_THRESHOLD`,
kobo `5_000_000`, configurable per tenant, and settable to zero to gate every one).
Below the threshold they still post directly. Refunds and write-offs are gated
**always**, at any amount.

So on one concession form, ₦2,000 posts and ₦400,000 must be submitted. The design has
to make that legible *before* the user commits. Suggested: as the amount field crosses
the threshold, the primary button changes from "Post" to "Submit for approval", with a
line of explanation. Do not surprise them at submit time.

### The rationale, for your copywriter
A purchase of ₦400,000 still buys something. A waiver of ₦400,000 is income given
away. That is why finance's bar (₦50,000) sits far below procurement's senior bar
(₦500,000).

---

## 2. Refunds and write-offs are now actually gated

**Was:** these had submit endpoints and handlers from the very start, but finance
published no approval templates, so `approval_required` answered `false` and both
posted directly. The gate existed and never fired.
**Now:** the ladder is published for every tenant, so it fires.

### What breaks
No API changed. **The behaviour did.** Any screen that posts a refund or a write-off
directly will start receiving the same 400 refusal as item 1. If your app already has
the submit flow wired but hidden behind a "does this need approval" check, that check
will now return true and the flow should light up on its own; test it.

The bulk refund endpoint has its own message when any document in the batch is gated:

```json
{"action": "One or more refunds are approval-gated; submit this batch for approval instead of posting it."}
```

Design note: a mixed batch is refused **whole**, not partially posted. The UI should
say which rows caused it, and offer "submit the batch" as the one-click alternative.

---

## 3. Vendor bills with no purchase order are refused by default

**Was:** `allow_non_po_invoices` defaulted to `true`, so any bill could be raised with
no PO. Nothing three-way matches such a bill, so approval was its only control.
**Now:** the default is `false`, **and migration 0027 flipped every tenant that already
exists** - deliberately, since those are the ones the change protects.

### What to build
1. **The vendor bill form** must stop offering "no purchase order" unless the setting
   is on for this entity. Read it from the procurement settings endpoint.
2. **The procurement settings screen** must expose `allow_non_po_invoices` as a toggle.
   Without it in the UI there is now no way for a school that genuinely bills without
   orders to reach the setting at all. This is the part that must not be skipped.

Copy for the toggle, close to the backend's own help text:

> **Allow bills with no purchase order.** Off by default. A non-PO bill has no ordered
> quantity, no receipt and no agreed price to check against, so approval is its only
> control.

### Related, for the same screen
Three-way match has **three** blocking outcomes, not two: `UNDER_RECEIVED`,
`OVER_BILLED` and `NON_PO_BLOCKED`. `PRICE_VARIANCE` does **not** block - the GR/IR
account clears at the receipt basis and the difference lands in purchase price variance
(account 5160). Overriding a blocking outcome needs its own permission,
`procurement.vendor_invoice.override_variance`. If your match screen currently implies
a price variance needs an override, that is wrong; it does not.

---

## 4. A new school's approval ladders arrive deliberately unstaffed

**Was:** publishing a tenant's approval ladders was a management command somebody had
to remember. A tenant whose ladders were never published had **no maker-checker at
all** - one person could send a whole salary batch to the bank.
**Now:** spend, payout and adjustment ladders are published inside the same transaction
that creates a tenant's books. They arrive **blocked, not open**: the approving roles
are created with **nobody appointed**, and the stages never auto-skip.

### What this looks like without UI work
A brand-new school's very first requisition, payout batch or adjustment parks
immediately, and the screen says nothing useful. It reads as broken software.

### What to build
1. **An onboarding step: "Appoint your approvers."** List the seeded roles and let an
   administrator assign people. The role keys:

   | Role key | Approves |
   |---|---|
   | `procurement-approver` | Requisitions, purchase orders, vendor invoices, vendor payments |
   | `procurement-senior-approver` | The same, at or above ₦500,000 |
   | `payout-approver` | Payout batches (one stage) |
   | `finance-adjustment-approver` | Refunds, write-offs, concessions, credit notes |
   | `finance-senior-adjustment-approver` | The same, above the senior threshold |

2. **A parked-document state that explains itself.** Whenever a document is parked, the
   `approval` block carries `requirement` - a finished sentence naming what to do. Show
   it. Do not render a bare "Pending approval" chip; that is the state the user cannot
   act on and cannot diagnose.

3. **The release affordance.** A parked document can be continued without approval by
   the person who submitted it (or platform staff). This is a deliberate product
   decision, not an accident: surface it as an explicit, logged action with its own
   confirmation, never as a quiet fallback.

Note for QA: a provisioner that fails **rolls the whole entity back**. There is no such
thing as a tenant with books and no ladders. You will never see a half-provisioned
school.

---

## 5. The period close checklist gained two rows, with two different severities

**Was:** the close ran without procurement's payables checks. The seam for them was an
argument only a unit test ever passed, so a period could be sealed over an AP
sub-ledger that disagreed with its control account and **report success**.
**Now:** dependent apps register their checks and the close runs them.

Endpoint unchanged: `POST /v1/finance/periods/<id>/close/`.

### The two new checklist items
| `name` | `blocking` | Meaning |
|---|---|---|
| `ap_reconciled` | `true` | The AP sub-ledger must equal its control account. Drift stops the close. |
| `grir_explained` | `false` | The GR/IR clearing balance. **A warning only.** |

Each item is `{name, passed, blocking, detail}`. `detail` is human-readable, e.g.
`"sub-ledger 1234500 vs control 1234000 kobo"` or `"GR/IR clearing balance 480000 kobo
(received not invoiced, or invoiced not received)"`.

### What to build
**Render `blocking: false` differently from `blocking: true`.** If a failed warning is
drawn like a failed blocker, month-end will stop for a balance that is entirely
legitimate: goods received late in the month and not yet billed leave a GR/IR balance
by design. The item exists so nobody closes without *seeing* the number, not to prevent
closing.

Two more behaviours worth handling:
- Both checks return **nothing at all** for an entity with no vendors. A school that
  has never bought anything should not carry a meaningless row on its close screen, so
  render whatever comes back rather than a fixed list.
- A check that **raises** is reported as failed, not omitted. A check that cannot
  answer is not one that passed.

---

## 6. Stock is now held per location (the largest piece of design work)

**Was:** a single pool per entity. One on-hand quantity, one value and one average cost
per item, however many places the school actually kept the goods. A two-campus school
could issue at the annex against stock physically standing at the main store, because
the availability check read the entity total.
**Now:** a stock location is a first-class record, optionally tied to a branch, and
every item has a balance row **per location** with its own quantity, value and
weighted-average cost.

**The item's own totals still exist as the roll-up across locations**, so every existing
entity-level screen keeps working and every existing valuation number is unchanged.

### 6.1 The rule that outranks the rest
**A school with one store must not see any of this.** Resolve the entity's active
locations first:
- **Zero or one:** render nothing. No picker, no column, no "All locations" chip, no
  empty state. The school never learns the concept exists.
- **Two or more:** show the control.

This is a platform rule, not a nicety: branch-optional and multi-branch schools must
both look finished, and an empty column is worse than no column.

### 6.2 New endpoints

**`GET /v1/procurement/stock-locations/`** - permission `procurement.stock.view`.
Paginated, default first then by code. Query param `is_active`.

```json
{
  "id": 12, "code": "MAIN", "name": "Main store", "description": "",
  "branch_id": null, "branch_name": null,
  "is_default": true, "is_active": true,
  "created_at": "…", "updated_at": "…"
}
```

**`POST /v1/procurement/stock-locations/`** - permission `procurement.stock.manage`.
Body: `code`, `name`, optional `description`, `branch` (id **or** branch code, or omit
for an entity-wide store), `is_default`, `is_active`.

- The **first** location an entity creates is forced to be the default.
- `is_default: true` **moves** the flag off the previous default. Render it as a radio
  or a "Make default" action, never a free checkbox.
- Duplicate `code` in an entity returns 400 on the `code` field.
- Booleans must be real JSON booleans, not `"true"` strings.

**`GET` / `PATCH /v1/procurement/stock-locations/<id>/`** - PATCH accepts `name`,
`description`, `branch`, `is_default`, `is_active`. Two refusals, both on `is_active`:

> "This location still holds stock. Move it out first."
> "The default location cannot be deactivated. Make another location the default first."

The first deserves a link straight to that location's balances.

**`GET /v1/procurement/stock-balances/`** - permission `procurement.stock.view`.
Query params: `stock_item` (id or code), `location` (id or code), `held_only=true`
(hides rows that are zero quantity **and** zero value).

```json
{
  "id": 44,
  "stock_item_id": 7, "stock_item_code": "PAPER-A4", "stock_item_name": "A4 Paper",
  "location_id": 12, "location_code": "MAIN",
  "on_hand_qty": "120.0000", "stock_value": 4800000, "unit_cost": 40000,
  "updated_at": "…"
}
```

`stock_value` and `unit_cost` are kobo. **`unit_cost` is that location's own weighted
average and may legitimately differ from another location's for the same item.** Do not
treat a mismatch as a data error or try to reconcile it in the UI.

### 6.3 Changed endpoints

**Issue** `POST /v1/procurement/stock-items/<id>/issue/` and **adjust**
`POST /v1/procurement/stock-items/<id>/adjust/` take a new optional body field
**`location`** (id or code).

With exactly one active location the field is optional and resolves automatically. With
**more than one, a call that names none is refused** - error code `STOCK_ERROR`:

> "This entity has more than one stock location, so a movement must say which one.
> Pass a location (default: MAIN)."

This was a deliberate product decision. Silently drawing from the main store when
somebody meant the annex is a quieter version of the bug locations exist to fix. **Make
the field required in the form whenever there is more than one location.**

Other `STOCK_ERROR` messages: `"Stock location 'X' is not active."` and `"This entity
has no stock location. Create one before moving stock."` (the latter should never
appear; every entity is provisioned with one).

**Insufficient stock now names the location.** Error code `INSUFFICIENT_STOCK`, with the
item rendered as `PAPER-A4@ANNEX`. An issue of 500 at ANNEX fails when ANNEX holds 200,
even if MAIN holds 900. The user's real intent is usually to issue from the other
store, so the error should say where it fell short and ideally offer "MAIN holds 900".

**Movements** `GET /v1/procurement/stock-movements/` - new `location` query param;
serializer gained `location_id` and `location_code`. **`balance_qty` and `balance_value`
are now the running balance at that location, not the entity** - relabel the ledger's
balance column.

**Reorder report** `GET /v1/procurement/reports/stock-reorder/` and **valuation report**
`GET /v1/procurement/reports/stock-valuation/` - new `location` query param; the
response echoes `location` (code, or `null` for the whole entity). **With no location
the numbers are identical to before**, so existing report screens are safe and the new
filter can ship separately.

**Goods receipt** - no API change. A GRN now lands stock at the receiving **branch's**
store, falling back to the entity default where that branch has none. Worth showing on
the confirmation: "Received into: Annex Store."

### 6.4 What to build
1. **A locations admin screen.** Code, name, campus (branch, or "Entity-wide"),
   default, active. Create, edit, make default, deactivate. Low traffic; a modal form
   is fine.
2. **A per-location breakdown on stock item detail.** Keep the headline totals as they
   are (they are the roll-up); below them, when there is more than one location, a
   table from `stock-balances/?stock_item=<id>`: Location, On hand, Unit cost, Value.
3. **Location on the issue and adjust forms**, required when there is more than one,
   pre-filled with the default, or with the location the user arrived from.
4. **A location filter on the ledger and both reports.** When a location is selected on
   the valuation report, label the total as that store's and say so: "Annex store only.
   The entity total is elsewhere."
5. **Copy for three refusals:** issuing with no location where several exist; issuing
   more than the location holds when another has enough; deactivating a location that
   still holds stock.

### 6.5 Migration state, so you design for real data
Every existing stock-holding entity has a location coded **`MAIN`**, balances opened for
every item carrying quantity or value, and every historical movement stamped with
`MAIN`. **There is no null-location state on existing data.** New entities get a `MAIN`
default with their books.

A school **already running two campuses** will see all its stock at `MAIN`. The remedy
is operational, not automatic. A prompt on the locations screen would be well placed:
"All your stock is currently at MAIN. Create your other stores, then move the opening
balances across."

### 6.6 Do not build a Transfer button
**There is no transfer document.** Moving stock between locations today means an issue
at the source and a receipt at the destination, and the receipt re-prices the stock
rather than carrying its cost across. It is recorded as a gap in MRD v2.13 and M24 FRD
v1.1 and is the likely next backend change; when it lands it will be one document with
its own endpoint. Building a Transfer button now is wasted work.

---

## 7. Two platform-console endpoints with no UI at all

Both are on the workflow template viewset, both **platform-tenant only**, and both are
configuration-only: no documents, no approvals, no people.

**`GET /v1/workflow/templates/<id>/adoption/`** - who runs this shared template as
published, and who runs their own.

```json
{
  "template": {"id": 3, "name": "…", "document_type": "…", "code": "…", "updated_at": "…"},
  "customer_count": 44, "following_count": 40, "adjusted_count": 4,
  "adjusted": [
    {"tenant_slug": "corona", "tenant_name": "Corona Secondary School",
     "template_id": 91, "branch": null, "stage_count": 3, "updated_at": "…"}
  ]
}
```

Counts **tenants, not templates**: a tenant with both a branch-level and a tenant-level
version has still adjusted it once. A tenant that has never opened the template counts
as following it - that is what inheriting means.

**`GET /v1/workflow/templates/<id>/compare/?with=<template id>`** - how one tenant's
version differs from the shared one.

```json
{
  "template_fields": {…},
  "stages": {"added": [...], "removed": [...], "changed": [...]},
  "routes_differ": false,
  "identical": true
}
```

"Added" and "removed" are said from the platform reader's point of view: a stage only
the tenant has is one they *added*. Stages match by code, so a renamed label reads as a
changed field rather than a stage removed and another added.

Refusals: **403** `PLATFORM_ONLY` ("Only the platform can see how tenants have adjusted
a template."), **400** `NOT_PLATFORM_TEMPLATE` ("Only a shared template has tenant
versions to compare.").

### What to build
The purpose is to tell the person editing a shared template whether they are changing
the path for forty tenants or for four. So: an adoption count on the template editor,
and a "see how they differ" drill-down. Put the count where they cannot miss it before
they hit save.

---

## 8. Support tickets carry the context the user was in

`context` is now on the ticket serializer (read-only on read, accepted on create) and
is a strict allowlist of exactly four keys - anything else is rejected:

| Key | Format |
|---|---|
| `guide_id` | lowercase slug, up to 120 chars |
| `route_pattern` | normalized route starting `/`, **no digits, no query string, no fragment** |
| `product_area` | one of a fixed list (Account, Audit and security, Console, Data imports, Exports, Finance, Health, Notifications, Organogram, Permissions, Platform health, Procurement, Roles, School management, Settings, Support, Tasks, Users, Workflow) |
| `app_version` | version string, up to 40 chars |

### What to build
1. **Attach context automatically** when a user raises a ticket from inside the app.
2. **`route_pattern` must be the pattern, not the URL.** Send
   `/finance/invoices/:id/`, never `/finance/invoices/8842/`. Digits are rejected
   outright, on purpose: a parameter placeholder is the proof that record identifiers
   were stripped. Do not append query strings.
3. **Show it on the support console ticket view**, so an agent can see where the person
   was without asking.

---

## 9. A role picker that used to fail should now work

`workflow.template.manage` now also carries the reads it depends on:

- the **role list** endpoint (names, keys and counts only - role detail and every write
  still require the role keys themselves);
- the **approver groups** list (writing a group still needs the group's manage key).

An approval stage names the role that approves it, and there was no way to name one
without seeing the list. The old alternative was to make every template manager a role
administrator, which grants far more than the job needs.

Also, **`/me` now returns `tenant.kind`** alongside `slug` and `name`. Use it to tell a
platform operator from a school - notably to decide whether to show item 7's endpoints -
instead of matching on the slug.

### What to build
Probably nothing new. If your workflow template builder has a role or group picker that
was silently 403-ing and rendering empty, it should populate now. Worth a look: this may
close a bug you already have on file.

---

## Permission keys referenced

| Action | Key |
|---|---|
| View stock locations, balances, movements | `procurement.stock.view` |
| Create/edit locations and stock masters | `procurement.stock.manage` |
| Issue stock | `procurement.stock.issue` |
| Adjust stock | `procurement.stock.adjust` |
| Reorder and valuation reports | `procurement.report.view` |
| Override a blocking three-way match | `procurement.vendor_invoice.override_variance` |
| Submit a concession | `finance.concession.submit` |
| Post a concession | `finance.concession.post` |
| Submit a credit note | `finance.creditnote.submit` |
| Post a credit note | `finance.creditnote.post` |
| Manage workflow templates (now carries role and group reads) | `workflow.template.manage` |

## Suggested split of work

1. **One dev, finance approvals:** items 1, 2 and 4. They are one coherent piece - the
   gate, the documents it now covers, and the people who have to be appointed for it to
   clear.
2. **One dev, procurement and stock:** items 3 and 6. Both live in the procurement area
   and share the settings screen.
3. **Smaller, independent:** item 5 (close checklist severities), item 7 (platform
   console), item 8 (ticket context). Item 9 is a check, not a build.
