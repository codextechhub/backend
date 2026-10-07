# Notes

## Payroll: for the accountant, before any school relies on payroll

- the 2026 tax bands and reliefs;
- the pension, housing fund, NSITF and ITF rates, and when ITF applies;
- the list of pension administrators;
- the state tax-office names.

## Records: for the accountant

- how long financial records must be kept (built as 6 years after the end of the
  fiscal year; confirm 6 or 7).

## Paystack: four details to check with a test-mode call (CodeX's TEST key)

Paystack's public docs don't state these. The code already handles each one safely,
so nothing is blocked; one trial call with the test key settles them.

- Whether a settlement that is still "processing" has already left the Paystack
  balance (the daily held-money check treats a gap it fully explains as "not
  measured").
- What Paystack calls a dispute nobody answered in time ("auto-accepted" is
  assumed, and read as lost).
- Whether a branch can bear Paystack's fee on a dedicated virtual account
  (the fee-bearer setting is sent; the docs only confirm it for normal payments).
- The exact layout of dispute and refund notifications (whether they carry the
  original payment as "transaction" or "transaction_reference").
