## Undone

# Finance backlog — remaining gaps from Crestfield-Finance/Finance-Gap-Checklist.md (all `G`-marked stretch items; core GL/AR/P2P/Payments shipped & tested). Build-first triage is DONE; these are the value-add / statutory / automation layer and do NOT block frontend work.

## AR cycle (vs_finance receivables)
# - Dunning / automated payment reminders
# - School-fee billing adapter (fee categories + structures → emit generic invoices, behind a module flag)

## Procurement (vs_procurement)
# - RFQ + vendor quotations + award → PO
# - Item catalog (preferred vendor, lead time, default tax/GL)
# - Vendor contracts (renewal/expiry alerts, milestones)
# - AP cash-requirements forecast
# - Route PR/PO/invoice approvals through vs_workflow with thresholds (today approve is a direct endpoint)
# - GR/IR monthly aging report (only point-in-time grir_balance exists today)

## Banking / close (vs_finance)
# - Inventory / stock ledger (valuation, reorder, stock movement) — catalog + GRN exist, no valuation
# - Petty cash management
# - Tax remittance / filing workflow (FIRS VAT/WHT, PAYE to State IRS, pension to PFA)

## Reporting
# - Statement of changes in equity
# - Procurement analytics (spend analysis, vendor performance, PR→payment cycle time)
# - Statutory export packs (IFRS-for-SMEs lines, FIRS/CAC-ready)

## Payments (vs_payments)
# - Bulk payout / disbursement file ("generate bank file" made real)
# - Daily settlement reconciliation vs bank feed
# - Open-banking statement feed (Mono/Okra) — optional, automates bank rec

## Done

# 0f. Finance backlog — AR customer statements of account. New report `customer_statement(customer, *, start_date=None, end_date=None)` in reports.py builds a dated ledger of one customer's POSTED AR documents (invoices/debit notes/refunds debit; receipts/credit notes/concessions credit) with a running balance: opening_balance folds all movements before start_date (None = from inception, zero opening), entries cover [start_date, end_date] (end defaults to today), closing_balance = opening + debits − credits, plus an aging block over the customer's still-open invoice balance_due as at end_date. Write-offs clear invoices internally and aren't itemised (aging reflects live balances). REST at GET /v1/finance/reports/customer-statement/ (?entity=&customer=<code|id>&start=&end=, rbac finance.report.view, kobo+naira money, ?export=csv|xlsx|pdf) via CustomerStatementView in views_ar.py. No model change → no migration. 2 new tests, 92 vs_finance tests pass, check clean, no drift.

# 0e. Finance backlog — AR installment plans + concessions. Concessions (discount/waiver/scholarship, doc CNC) post Dr 4910 Discounts & Concessions Allowed / Cr AR and clear the invoice via amount_credited (a policy-grounded targeted credit; SCHOLARSHIP is the domain-neutral name for a school bursary). PaymentPlan (doc PPL) is a pure scheduling overlay — never posts: splits a receivable into evenly-divided dated installments (WEEKLY/FORTNIGHTLY/MONTHLY/QUARTERLY, remainder on the last), DRAFT→ACTIVE→COMPLETED/CANCELLED; refresh_plan_progress distributes the linked invoice's settled_amount across installments oldest-first and auto-completes. New 4910 contra-income account seeded. Services in installments.py, REST in views_ar.py (/v1/finance/ concessions + payment-plans w/ activate/refresh/cancel), migration 0011. 7 new tests, 90 vs_finance tests pass, check clean, no drift.

# 0d. Finance backlog — AR adjustments: credit/debit notes (CRN/DRN tokens via per-instance DOC_TYPE), customer refunds (RFD), bad-debt write-offs. New Invoice.amount_credited tracks non-cash reductions; settled_amount = amount_paid + amount_credited drives balance_due / payment status (cash logic unchanged when amount_credited=0). CREDIT note Dr revenue+output tax / Cr AR then optional oldest-first allocation against open invoices; DEBIT note reverses (Dr AR / Cr revenue+tax, cannot allocate); refund Dr AR / Cr bank; write-off Dr bad-debt 5300 / Cr AR. Services in credit_notes.py, REST in views_ar.py (/v1/finance/ credit-notes, refunds, invoices/<pk>/write-off), migration 0010. 5 new tests, 83 vs_finance tests pass, check clean, no migration drift.

# 0c. Finance Phase 6 — Payment integration (NEW app vs_payments, mounted /v1/payments/). Both directions: collections (money-in) + payouts (money-out) end-to-end, behind a provider-neutral interface with real Paystack + OPay HTTP clients (stdlib urllib) and a deterministic FakeProvider for tests. Confirmed collection → vs_finance post_payment (Dr bank/Cr AR); confirmed payout → vs_procurement post_vendor_payment (Dr AP/Cr bank/Cr WHT). Idempotent webhooks (dedupe_key + select_for_update + terminal short-circuit → retried event never double-books). Public signature-verified webhook receiver. Append-only PaymentEvent audit. 17 tests pass, check clean, no vs_payments migration drift. Credential-sourcing guide for Paystack/OPay delivered to user.

# 0b. Finance Phase 5 — Financial statements (Income Statement, Balance Sheet with unclosed net income → retained earnings, Cash Flow classified operating/investing/financing) in reports.py; DRF REST API at /v1/finance/ (entity-scoped via ?entity=<id|code>: entities/accounts/periods/journals/invoices lists, journal detail, post/reverse/close actions, six report endpoints) matching the platform envelope + RBAC; exports (CSV/Excel/PDF via ?export= on the report endpoints). 78 tests pass, check clean, no migration drift.

# 0a. Finance Phase 4 — Banking + reconciliation, expense claims, payroll (accrue/disburse), budget + variance, fixed-asset register + straight-line depreciation, period close (4-state lock, checklist, injectable extra_checks). All in vs_finance. 64 tests pass, check clean, no migration drift.

# 0. Finance Phase 3 — Procure-to-Pay (vs_procurement): PR→PO→GRN→VendorInvoice→VendorPayment with GR/IR clearing, 3-way match, WHT, AP aging/reconciliation. 10 tests pass.

# 1. A Check for if school admin and branch admin already exist in the users database and throw validation error if yes
# 2. Make module, resource and action in the permission model models of their own that form the key of a permission.
# 3. Add school/branch scoping to school role template
# 4. uid for users — unique per school (school users) and unique across Vision Staff. Starts at 10, auto-increments, uneditable.
