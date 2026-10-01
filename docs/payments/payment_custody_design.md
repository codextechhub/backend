# payment_custody_design

**Status:** phase A (build steps 1 to 3) is built; held mode's own machinery and
switching (steps 4 and 5) are not. Every decision below is agreed. Facts about
Paystack marked *(confirm)* are to be checked against Paystack's documentation or
account manager before a tenant takes payments directly; the adapter's docstring
(`vs_payments/providers/paystack.py`) lists each field it relies on.

Online money has to reach the school's own bank, and the books have to say where
it is at every moment. Today every tenant's checkouts settle into one Codex
Paystack balance (one platform secret key, no subaccount or split), each school's
books debit its own bank the moment a parent pays, and a gateway payout can spend
the shared balance with no check that the paying school collected that money.

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
   Nothing is surcharged to the payer.
5. **Codex takes no fee per payment.** No split to the main account.
6. **The held-mode settlement schedule is a tenant setting**: every N days (1 to 7,
   default 1).
7. Switching from held to direct first settles everything Codex holds for that
   tenant; the switch takes effect at the next month start.
8. Held mode needs a legal opinion on holding customer funds (by inference, a CBN
   licence may be required) before any tenant is offered it.

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
- **Refunds and chargebacks** come out of Codex's main balance *(confirm)*, so a
  direct-mode refund needs the branch to fund it first, or is recorded as owed by
  the branch to Codex. Decided at the step that builds refunds.
- **No online payouts.** The payout screens are absent for a direct-mode tenant.

---

## 4. Held mode

- **Checkouts** name no subaccount; money settles to Codex's own bank from Codex's
  Paystack balance, as today.
- **Codex's books** keep one liability per branch of each held-mode tenant: "Held
  for Bright Star Lekki". A confirmed payment raises it; a settlement to the branch
  or an online payout for the branch lowers it. It reconciles daily to the Paystack
  balance.
- **Settlement run.** Every N days (the tenant's setting) Codex pays each branch
  what cleared into Codex's balance for it, less fees, by transfer to the branch's
  collection bank account, approved like a payout. The branch books it as the
  clearing-to-bank step in section 2.
- **Online payouts** for a branch are refused above that branch's held balance, and
  the check happens under a lock at dispatch.

---

## 5. Switching

- A tenant's mode, its effective month start, and its settlement interval are a
  payments setting, changed by a whole-tenant administrator.
- **Direct to held:** takes effect at the next month start. Branch subaccounts stay
  on record but are no longer named on checkouts.
- **Held to direct:** a final settlement run pays out every branch's held balance;
  the switch takes effect at the next month start only once every held balance is
  zero. Subaccounts must exist for every branch first.

---

## 6. Build order

Steps 1 to 3 are phase A and are built; steps 4 and 5 are phase B.

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
4. **Held mode.** Codex-side liability per branch, the settlement run on the
   tenant's interval, and the payout funds check at dispatch.
5. **Switching,** including the final settlement from held to direct. Until then a
   dedicated virtual account created while a tenant was held keeps settling to
   Codex's balance after the tenant moves to direct.
