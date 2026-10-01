# payment_custody_design

**Status:** built. Phase A (build steps 1 to 3) and phase B (held mode's own
machinery and switching, steps 4 and 5) are in the code. Every decision below is
agreed; the open questions phase B raised are in section 7. Facts about
Paystack marked *(confirm)* are to be checked against Paystack's documentation or
account manager before a tenant takes payments directly; the adapter's docstring
(`vs_payments/providers/paystack.py`) lists each field it relies on.

Online money has to reach the school's own bank, and the books have to say where
it is at every moment. Before this design every tenant's checkouts settled into
one Codex Paystack balance (one platform secret key, no subaccount or split), each
school's books debited its own bank the moment a parent paid, and a gateway payout
could spend the shared balance with no check that the paying school collected
that money.

Each tenant chooses how its online money is held. The choice is per tenant,
applies to all its branches, and switches only at the start of a month.

| Mode | Where a parent's money goes | Who can spend it online |
| --- | --- | --- |
| **Direct** | A Paystack subaccount per branch, under Codex's merchant account, settles each payment straight to that branch's bank. | Nobody: it never sits in a Paystack balance. Suppliers are paid from the branch's bank and recorded in the books. |
| **Held** | Codex's Paystack balance. Codex's own books record what it owes each branch, and a settlement run pays it out on the tenant's schedule. | That branch's own held balance, checked before any payout leaves. |

---

## 1. Decisions

1. Both modes exist. A tenant picks one; it switches only at the start of a month.
2. **Direct uses Codex's merchant account with one subaccount per branch.** Codex
   stays merchant of record (and so answers to Paystack for disputes and
   chargebacks); a school gives only each branch's bank details.
3. **Direct-mode tenants have no online supplier payouts.** They pay suppliers from
   their own bank and record the payment, which the books already support. Online
   payouts are for held-mode tenants only.
4. **The school bears Paystack's fee.** It is recorded as the branch's bank charge.
   Nothing is surcharged to the payer. In held mode the branch also bears Paystack's
   transfer fee on each settlement: it is deducted from what is sent and booked as
   the branch's bank charge, and Codex's provider balance falls by the whole.
5. **Codex takes no fee per payment.** No split to the main account.
6. **The held-mode settlement schedule is a tenant setting**: every N days (1 to 7,
   default 1).
7. Switching from held to direct first settles everything Codex holds for that
   tenant; the switch takes effect at the next month start.
8. Held mode needs a legal opinion on holding customer funds (by inference, a CBN
   licence may be required) before any tenant is offered it.
9. **Codex's own books carry what it holds.** The platform tenant's ledger entity
   (tenant `codex`, branch Lagos) keeps one control liability, client funds held
   (mapping `CLIENT_FUNDS_HELD`, starter code 2180), with a sub-ledger row per
   held tenant branch, the asset behind it, the provider balance
   (`PROVIDER_BALANCE`, 1127), and what a branch owes Codex when a chargeback took
   more than it held (`CLIENT_FUNDS_OWED`, 1128).
10. **Codex starts and approves settlements.** A held-mode settlement is Codex
    paying out money it owes, so it is a payout in Codex's books (branch Lagos),
    drawing on Codex's provider balance: a Codex finance operator submits it and two
    Codex people approve it (two distinct real people, always), through Codex's own
    settlement route. When the transfer is confirmed the school's books record the
    money arriving automatically; nobody at the school submits or approves anything.
    The school sees its settlements read-only, narrowed to its branches; Codex's
    operators see every tenant's settlements due.
11. **Direct-mode refunds and chargebacks are not done online.** A refund is paid
    from the branch's bank and recorded; a chargeback is recorded and raised to the
    tenant's finance staff and Codex operators, never booked automatically.
    **A chargeback on held money** lowers the branch's held balance at once, booked
    in both books; if the branch holds less, the shortfall is owed by the branch to
    Codex and taken from its next settlement.
12. **Moving from held to direct reissues virtual accounts.** Each customer with a
    dedicated virtual account settling to Codex's balance gets a new one against
    the branch's subaccount, and the old number is retired: money still paid into
    it is credited to its customer and passed on in the next settlement run.
13. **Go-live opening balances** are recorded by a Codex operator from Paystack's
    records, once per branch (`record_held_opening_balance`), audited under that
    operator; a second for the same branch is refused.

---

## 2. What happens to one payment (both modes)

Mrs Adeyemi pays N180,000 online for Tunde's fees at Bright Star Lekki.

| Moment | Books (Lekki) |
| --- | --- |
| Paystack confirms the payment | Dr Gateway clearing N180,000, Cr Receivable N180,000. The invoice is paid. |
| The money reaches a bank (direct: Lekki's bank; held: Codex pays Lekki in the settlement run) | Dr Bank N178,000, Dr Bank charges N2,000, Cr Gateway clearing N180,000 |

"Gateway clearing" (the `FinanceAccountMapping` key `GATEWAY_CLEARING`, starter code 1125) holds
money a payment provider has confirmed but that has not reached a bank. It is a
current asset, shown per branch, and should read zero once every settlement is in.
A clearing balance older than the provider's settlement cycle is a close-check
warning.

The fee is known per transaction from the provider's verify response or its
settlement report *(confirm the field)*; the settlement match books it.

---

## 3. Direct mode

- **Subaccounts.** Each branch's collection bank account (the branch's "pay to"
  account) gets a Paystack subaccount created from its bank code and account
  number, with no percentage to Codex *(confirm the required fields)*. The
  subaccount code is stored on that bank account. A branch cannot take online
  payments until its subaccount exists.
- **Every checkout names the branch's subaccount** and asks for the fee to be borne
  by the subaccount *(confirm the `bearer` value)*. Dedicated virtual accounts are
  created against the same subaccount *(confirm)*.
- **Settlement.** Paystack settles each branch's share to its bank on its own cycle,
  usually the next working day *(confirm)*. The bank statement line is matched to
  the clearing balance of the payments it carries (the provider's settlement
  report lists them *(confirm)*), booking the fee.
- **Refunds and chargebacks** made through Paystack would come out of Codex's main
  balance *(confirm)*, so a direct-mode tenant does neither online: it refunds
  from the branch's bank and records the refund, and a chargeback is raised to
  people rather than booked (section 6).
- **No online payouts.** The payout screens are absent for a direct-mode tenant.

---

## 4. Held mode (built: `vs_payments/held.py`)

- **Checkouts** name no subaccount; money settles to Codex's Paystack balance.
  Whether a payment is held is decided by how it was routed, not by today's mode:
  a checkout or virtual account that named no subaccount is held
  (`CollectionIntent.held_by_platform`, set on confirmation).
- **Codex's books** keep one liability per branch of each held-mode tenant: "Held
  for Bright Star Lekki". It is a sub-ledger, `HeldMovement` rows keyed by tenant
  and branch summed on one `HeldBalance` row per branch, under the control account
  `CLIENT_FUNDS_HELD` in Codex's chart. A confirmed payment raises it by what
  reached the Paystack balance (the payment less the fee): Dr provider balance,
  Cr client funds held, in Codex's books, branch Lagos, alongside the school's own
  Dr gateway clearing. A settlement or an online payout for the branch lowers it
  when its transfer is dispatched, and a failed transfer gives it back. Each
  movement posts Codex's journal in the same transaction when Codex's books can
  take it; otherwise the movement stands, says why (`journal_error`), and the
  daily run posts it later. The sub-ledger, not the journal, is what a payout is
  checked against. Both control accounts are kept by the held-funds ledger, so a
  hand journal cannot touch them, and only Codex's books list or map the two
  roles. Reconciling the sub-ledger to the actual Paystack balance is not built
  (section 7).
- **Settlement run** (`vs_payments.run_held_settlements`, daily at 06:30). For each
  branch with held payments not yet settled, every `settlement_interval_days`
  after its last settlement, it claims the payments confirmed before the start of
  the tenant's day (`CollectionIntent.held_settlement`) and builds a
  `HeldSettlement` for what they come to less their fees, capped at the branch's
  held balance (online payouts may have spent part of it), less Paystack's transfer
  fee (`HeldSettlement.transfer_fee`, from the provider's fee schedule), which the
  branch bears. That is a `SETTLEMENT` payout batch of one line in **Codex's**
  books (branch Lagos, paid from the provider balance) into the branch's collection
  account. A Codex operator lists what is due (`GET
  /v1/payments/platform/held-settlements/`) and puts it forward (`POST
  .../<id>/submit/`); Codex's `held-settlement` route asks two members of its
  `held-settlement-approver` group, and dispatch refuses fewer than two distinct
  real approvers whatever the amount. At dispatch the branch's held balance falls
  by what is sent plus the transfer fee, and Codex's books move the provider
  balance by the same. Once Paystack confirms the transfer, the school's books
  record Dr bank (what arrived), Dr bank charges (the payments' fees and the
  transfer fee), Cr gateway clearing in the branch, and the claimed payments leave
  clearing; nobody at the school acts. The school reads its settlements at `GET
  /v1/payments/held-settlements/`, narrowed to its branches, and its movements feed
  shows each as a `settlement` transfer into its bank, never money spent. A branch with a settlement
  still pending is left alone, a payment is claimed by one settlement only, and a
  failed transfer releases its payments for the next run, so no payment is paid
  twice. A settlement with nothing to send (payouts spent it) books only its fees.
  The branch's collection account must carry its bank code at Paystack
  (`BankAccount.settlement_bank_code`, saved when its subaccount is set up).
- **Online payouts** for a branch are refused above that branch's held balance:
  the check takes a lock on the branch's `HeldBalance` row inside the dispatch's
  claim, and a refused payout is marked FAILED with the reason (409
  `HELD_FUNDS_INSUFFICIENT`), sending nothing. A payout drawn on held money books
  Cr gateway clearing, not the bank the batch named, because the money left the
  Paystack balance.
- **Chargebacks on held money.** A `charge.dispute.create` (first event of a
  dispute) on a held payment lowers the branch's held balance by what the payer's
  bank took, once: Codex's books Dr client funds held (as far as the branch held
  it), Dr owed by clients (the rest), Cr provider balance; the school's books Dr
  payment chargebacks (`CHARGEBACKS`, 5520), Cr gateway clearing, in the branch.
  The receipt and invoice are left for finance staff to pursue the payer. A
  balance below zero is what the branch owes Codex: payouts are refused, and its
  next payments repay it before anything is settled to it again.
- **A dispute Codex wins** (`charge.dispute.resolve` with `resolution: declined`
  *(confirm the field and values)*) gives the chargeback back, once per dispute:
  the branch's held balance rises by it, repaying any shortfall it owes Codex
  first (Dr provider balance, Cr owed by clients, then Cr client funds held), and
  the school's books reverse their entry (Dr gateway clearing, Cr payment
  chargebacks). A dispute lost (`merchant-accepted`, `auto-accepted`) stays as
  booked. Both outcomes are audited (`PROVIDER_DISPUTE_RESOLVED`) and raised; at a
  direct tenant the outcome is only recorded and raised.
- **Daily reconciliation** (`vs_payments.reconcile_held_ledger`, 07:15). Codex's
  books say its Paystack balance should hold its provider balance account (which
  mirrors the held-funds sub-ledger) plus its own online takings still in transit,
  each less its fee; Paystack says what it holds (`GET /balance` *(confirm)*). Each
  day's comparison is recorded (`HeldReconciliation`: date, Paystack's figure, the
  books' figure and its parts, the difference, the tolerance). A difference above
  the platform setting `payments.held_reconciliation_tolerance_kobo` (default 0),
  or a provider balance account that differs from the sub-ledger, opens one
  system-health incident (`payments.held-ledger-mismatch`) and tells Codex's
  health and settlement operators once; the next agreeing check resolves it. A day
  Paystack could not be asked is recorded with its error and changes no incident.
  Codex staff read the checks at `GET /v1/payments/platform/held-reconciliations/`.
  The check assumes Paystack keeps Codex's balance rather than sweeping it to
  Codex's bank each day *(confirm the account setting)*.
- **Money already held** before the sub-ledger started is entered once per branch
  by a Codex operator from Paystack's records: `manage.py
  record_held_opening_balance --by <operator email>`; it is audited under that
  operator, and a second for the same branch is refused.

---

## 5. Switching (built: `held.apply_custody_switch`)

- A tenant's mode, its effective month start, and its settlement interval are a
  payments setting, changed by a whole-tenant administrator. A daily task
  (`vs_payments.apply_custody_switches`, 00:20) applies a pending change whose
  month has come.
- **Direct to held:** in force from its month start. Branch subaccounts stay on
  record but are no longer named on checkouts.
- **Held to direct:** not in force until the task applies it. On or after its
  month start the task builds a final settlement for every branch (everything
  confirmed so far, ignoring the interval) and waits, recording why in
  `pending_note`, until nothing is held for the tenant and every branch's
  collection account has its subaccount. Then the mode changes and every active
  virtual account still settling to Codex's balance is replaced by one against its
  branch's subaccount; the old number is `RETIRED`. A deposit into a retired number
  is booked to its customer, held, and passed on in the next settlement run, which
  visits any branch with held payments whatever its tenant's mode. An account that
  could not be replaced stays active and the task retries it daily.

## 6. Refunds and chargebacks (built)

- **Direct tenant refunds:** any refund recorded as paid online (finance `Refund`
  with method `ONLINE`) is refused for a direct tenant with 409
  `ONLINE_REFUNDS_NOT_OFFERED`, at drafting and again at posting
  (`custody.assert_online_refund_allowed`, registered with finance as a refund
  guard). Recording a refund paid from the bank is unaffected.
- **Chargebacks and provider refunds:** Paystack's `charge.dispute.*` and `refund.*`
  events are matched to the payment, recorded (`PROVIDER_DISPUTE_RECEIVED`) and
  sent to the tenant's holders of `payments.webhook.view` and Codex's holders of
  `payments.unattributed_webhook.view` (`payments.dispute_received`). A chargeback
  on held money is also booked (section 4); nothing else is.

---

## 7. Build order

Steps 1 to 3 are phase A and steps 4 and 5 are phase B; all five are built.

1. **Gateway clearing (both modes). Built.** A confirmed payment books Dr gateway
   clearing (mapping key `GATEWAY_CLEARING`, starter code 1125, a control account a
   hand journal cannot touch), Cr receivable, in the payment's branch, and stores the
   provider's fee on the collection. `POST /v1/payments/settlements/` matches a bank
   statement line to the payments it carries and books Dr bank (what arrived), Dr
   bank charges (the fee), Cr clearing (the payments) in the bank account's branch,
   reconciling the line; unmatching it in the bank reconciliation reverses the journal
   and puts the payments back in clearing. The settlement report proposes the day's
   payments each unmatched inflow carries. The period close warns (not blocking)
   when a payment has waited in clearing longer than the tenant's
   `clearing_stale_days` (default 7) before the period's end.
2. **Mode setting and settlement interval. Built.** `GET/PATCH
   /v1/payments/settings/custody/`, written only by a whole-tenant caller: `mode`
   (`DIRECT` or `HELD`, default `HELD`), stored as pending from the next month start;
   `settlement_interval_days` (1 to 7, default 1, read by step 4);
   `clearing_stale_days` (1 to 60, default 7). Moving to direct is refused until
   every branch's collection account has a subaccount.
3. **Direct mode. Built.** Each branch names one collection account
   (`is_primary_collection` is unique per branch), which its documents print as "pay
   to". `POST /v1/payments/subaccounts/` (whole-tenant) creates or refreshes the
   Paystack subaccount behind it (no percentage to Codex) and stores its code on the
   bank account. A direct tenant's checkouts and dedicated virtual accounts name the
   collection's branch's subaccount with `bearer: "subaccount"`; a branch without one
   is refused with a 409 (`COLLECTION_SUBACCOUNT_MISSING`). Payout creation and
   dispatch are refused with a 409 (`ONLINE_PAYOUTS_NOT_OFFERED`) telling the tenant
   to pay suppliers from the bank and record the payment. Every collection, virtual
   account, payout and payout batch carries the branch it belongs to; a batch whose
   lines leave two branches' banks is refused.
4. **Held mode. Built.** Codex-side liability per branch (section 4), the
   settlement run on the tenant's interval as Codex's own payouts approved by two
   Codex people, the transfer fee borne by the branch, the payout funds check at
   dispatch, chargebacks on held money, and the opening-balance command. `GET
   /v1/payments/held-settlements/` (`payments.report.view`, narrowed to the
   caller's branches) lists a tenant's settlements; `GET
   /v1/payments/platform/held-settlements/` and `POST
   .../<id>/submit/` (Codex staff, `payments.platform_settlement.view` /
   `.submit`) are Codex's; `GET /settings/custody/` shows each branch's held
   balance.
5. **Switching. Built.** Section 5, with the final settlement, the wait for zero
   and the virtual account reissue; refunds and chargebacks per section 6.

Open questions:

- **Paystack's balance sweep.** The daily reconciliation compares Codex's books
  with Paystack's available balance, which only holds if Paystack keeps the money
  rather than settling it to Codex's bank each day *(confirm the account setting)*;
  Codex's own online payouts to its own suppliers, if it makes any, are not yet in
  the books' figure.
- **Paystack field names to confirm:** the dispute resolution (`resolution`:
  `declined`, `merchant-accepted`, `auto-accepted`) and the balance response
  (`GET /balance`, a list of `{currency, balance}` in kobo).
- **Paystack's transfer fee schedule** is the adapter's own table *(confirm)*;
  the fee actually charged is not read back from Paystack.
- **The held balance when the dispatch-time debit and the confirmed amount
  differ** is corrected once at confirmation; a held settlement is lowered when its
  transfer is dispatched rather than when it is confirmed, so two transfers can
  never spend the same money.
