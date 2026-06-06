## Undone

## Done

# 0a. Finance Phase 4 — Banking + reconciliation, expense claims, payroll (accrue/disburse), budget + variance, fixed-asset register + straight-line depreciation, period close (4-state lock, checklist, injectable extra_checks). All in vs_finance. 64 tests pass, check clean, no migration drift.

# 0. Finance Phase 3 — Procure-to-Pay (vs_procurement): PR→PO→GRN→VendorInvoice→VendorPayment with GR/IR clearing, 3-way match, WHT, AP aging/reconciliation. 10 tests pass.

# 1. A Check for if school admin and branch admin already exist in the users database and throw validation error if yes
# 2. Make module, resource and action in the permission model models of their own that form the key of a permission.
# 3. Add school/branch scoping to school role template
# 4. uid for users — unique per school (school users) and unique across Vision Staff. Starts at 10, auto-increments, uneditable.
