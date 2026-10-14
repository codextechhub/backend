"""Paystack provider - collections + payouts + webhooks.  # Concrete adapter for the Paystack PSP.

Reference: https://paystack.com/docs/api/ . Base URL ``https://api.paystack.co``; every
call authenticates with ``Authorization: Bearer <secret_key>``. Amounts are in **kobo**
already (Paystack's NGN minor unit), so no conversion. Webhooks are signed with
``x-paystack-signature`` = HMAC-SHA512 of the raw request body using the same secret key.  # Use the raw body for signature verification.

All network I/O goes through :func:`vs_payments.providers.http.request_json`, which tests
patch - so this client is fully exercised without ever calling Paystack.  # Keep HTTP interactions centralized and testable.

Paystack facts this adapter relies on for settlement subaccounts and fees, checked
against Paystack's API reference (paystack.com/docs/api) on 2026-10-04:

* ``POST /transaction/initialize`` accepts ``subaccount`` (a subaccount code) and
  ``bearer`` (``account``, the default, or ``subaccount``), and ``bearer:
  "subaccount"`` makes the subaccount bear Paystack's fee, so the branch receives the
  payment less the fee and the main account keeps none.
* ``POST /dedicated_account`` accepts ``subaccount``, so a dedicated virtual account's
  deposits settle to that subaccount's bank account; whether ``bearer`` is honoured
  there is not stated in the reference (it is sent).
* ``GET /transaction/verify/<reference>`` reports the fee Paystack kept, in kobo, as
  ``data.fees``.
* ``POST /subaccount`` takes ``business_name``, the bank's code, ``account_number``
  and ``percentage_charge`` (the main account's share of each payment, 0 for none),
  and answers ``data.subaccount_code``; the resolved holder name is read from
  ``data.account_name`` when present. The reference names the bank field
  ``bank_code`` in its parameter list but sends ``settlement_bank`` in its own
  example, so both carry the code: a subaccount pointed at a new bank must never
  keep settling to the old one because one name was ignored.
* ``PUT /subaccount/<code>`` takes the same fields to point an existing subaccount
  at a new bank account.
* A subaccount settles to its bank on Paystack's own schedule, usually the next
  working day, as one amount per settlement for the payments it covers.

Paystack facts this adapter relies on for payment terminals, checked against
Paystack's Virtual Terminal API reference (docs-v2.production.paystack.co/docs/api/virtual-terminal)
on 2026-10-13:

* ``POST /virtual_terminal`` takes ``name`` and ``destinations`` (each a WhatsApp
  ``target`` number and a ``name``) as required, and optional ``metadata``,
  ``currency`` and ``custom_fields``; it answers ``id``, ``code`` (``VT_...``),
  ``name``, ``active``, ``currency``, ``destinations``, ``paymentMethods`` and
  ``integration``. ``GET /virtual_terminal/:code`` fetches one,
  ``PUT /virtual_terminal/:code/deactivate`` switches it off (the reference lists
  no way to switch it back on), and ``PUT`` / ``DELETE /virtual_terminal/:code/split_code``
  with a ``split_code`` body attach and remove a split.
* A payment on a terminal arrives as ``charge.success`` whose
  ``data.metadata.virtual_terminal.code`` is the terminal's code.
* ``POST /split`` takes ``name``, ``type`` (``percentage`` or ``flat``), ``currency``,
  ``subaccounts`` (each a ``subaccount`` code and a ``share``), ``bearer_type``
  (``subaccount``, ``account``, ``all-proportional`` or ``all``) and, for a
  ``subaccount`` bearer, ``bearer_subaccount``; it answers ``split_code``.

Not stated by the reference, and so unconfirmed until a live call settles them: whether
``destinations`` may be empty, where the terminal's payment page link or QR poster is
returned (only ``paymentMethods`` is named), the exact shape of the sender's name on a
transfer (read from ``data.authorization.sender_name``), and whether a split attached
to a terminal honours ``bearer_type: subaccount`` the way a checkout's ``bearer`` does.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import urllib.parse

from ..exceptions import ProviderError
from .base import (
    AccountResolution,
    CheckoutResult,
    CollectionStatusResult,
    Provider,
    SettlementRecord,
    SplitResult,
    SubaccountResult,
    RefundResult,
    TerminalResult,
    TransferResult,
    VirtualAccountResult,
    WebhookParseResult,
)
from .http import request_json

# Paystack transaction/transfer status string → our neutral status.  # Translate provider states into our domain.
_COLLECTION_STATUS = {
    "success": "SUCCEEDED",
    "failed": "FAILED",
    "abandoned": "ABANDONED",
    "reversed": "REFUNDED",
}  # Mapping from Paystack collection status to neutral status.
_TRANSFER_STATUS = {
    "success": "PAID",
    "failed": "FAILED",
    "reversed": "REVERSED",
    "abandoned": "FAILED",
    "pending": "PROCESSING",
    "otp": "PROCESSING",
    "processing": "PROCESSING",
}  # Mapping from Paystack transfer status to neutral status.


#: Paystack's refund ``status`` (Refund API reference: pending, processing,
#: needs-attention, processed, failed) mapped to the neutral one. ``needs-attention``
#: waits for the payer's bank details through Paystack's retry endpoint, so it is
#: not final, and neither is ``pending``.
_REFUND_STATUS = {
    "processed": "PROCESSED",
    "failed": "FAILED",
    "pending": "PROCESSING",
    "processing": "PROCESSING",
    "needs-attention": "PROCESSING",
}


# Group behavior for Paystack Provider.
class PaystackProvider(Provider):
    name = "PAYSTACK"  # Provider lookup key used by the registry.

    #: The dedicated-account bank used when nothing names one. Paystack's reference
    #: (``POST /dedicated_account``) gives ``wema-bank`` and ``titan-paystack`` as
    #: example slugs and states no default; a test key can use only ``test-bank``.
    LIVE_DEFAULT_PREFERRED_BANK = "wema-bank"
    TEST_PREFERRED_BANK = "test-bank"

    def __init__(self, *, secret_key: str, base_url: str = "https://api.paystack.co",
                 dedicated_account_bank: str = ""):
        self.secret_key = secret_key  # Bearer token used for all API requests.
        self.dedicated_account_bank = (dedicated_account_bank or "").strip()  # Deployment's chosen slug.
        self.base_url = base_url.rstrip("/")  # Normalize away a trailing slash once.

    # -- internals ---------------------------------------------------------- #  # Shared request helpers below.
    # Support the headers workflow.
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret_key}"}  # Standard Paystack auth header.

    # Support the post workflow.
    def _post(self, path: str, body: dict) -> dict:
        return request_json("POST", f"{self.base_url}{path}", headers=self._headers(),  # Send an authenticated POST.
                            body=body, provider=self.name)  # Include provider name for better error context.

    # Support the get workflow.
    def _get(self, path: str) -> dict:
        return request_json("GET", f"{self.base_url}{path}", headers=self._headers(),  # Send an authenticated GET.
                            provider=self.name)  # Include provider name for better error context.

    def _put(self, path: str, body: dict | None = None) -> dict:
        return request_json("PUT", f"{self.base_url}{path}", headers=self._headers(),
                            body=body, provider=self.name)

    def _delete(self, path: str, body: dict | None = None) -> dict:
        return request_json("DELETE", f"{self.base_url}{path}", headers=self._headers(),
                            body=body, provider=self.name)

    @staticmethod
    # Support the require ok workflow.
    def _require_ok(resp: dict):
        if not resp.get("status", False):
            raise ProviderError(resp.get("message", "Paystack request failed."),
                                provider="PAYSTACK")  # Tag the error with the provider name.
        return resp.get("data", {})

    def healthcheck(self) -> bool:
        # Balance is authenticated and read-only. Do not return its financial data.
        self._require_ok(self._get("/balance"))
        return True

    # -- collection --------------------------------------------------------- #  # Collection-side operations.
    def create_checkout(self, *, reference, amount, currency, customer_email="",
                        customer_name="", narration="", callback_url="", metadata=None,
                        subaccount=""):
        body = {
            "email": customer_email or "customer@example.com",  # Paystack expects an email value.
            "amount": amount,  # Amount is already in kobo.
            "currency": currency,  # Forward the requested currency as-is.
            "reference": reference,  # Use our merchant reference for idempotency.
            "callback_url": callback_url,  # Return URL after checkout.
            "metadata": {**(metadata or {}), "narration": narration,  # Preserve caller metadata.
                         "customer_name": customer_name},  # Attach the display name for support.
        }
        if subaccount:  # Settle to the branch's bank, the branch bearing the fee.
            body["subaccount"] = subaccount
            body["bearer"] = "subaccount"
        data = self._require_ok(self._post("/transaction/initialize", body))  # Start a hosted payment checkout.
        return CheckoutResult(
            reference=reference,  # Echo our merchant reference back to the caller.
            provider_reference=str(data.get("reference", reference)),
            checkout_url=data.get("authorization_url", ""),
            authorization_code=data.get("access_code", ""),
            status="PENDING",  # Hosted checkout is pending until verified.
            raw=data,  # Preserve the raw provider data.
        )

    def preferred_bank_slug(self, requested: str = "") -> str:
        """The bank slug a dedicated account is asked for, never empty.

        Paystack's ``preferred_bank`` takes a bank *slug* (``wema-bank``), not the
        numeric bank code used for transfers. The order is the caller's choice,
        then the deployment's (``PAYSTACK_DEDICATED_ACCOUNT_BANK``), then
        ``test-bank`` for a test key (the only bank a test key can use) or
        :attr:`LIVE_DEFAULT_PREFERRED_BANK` for a live one.
        """
        chosen = (requested or "").strip() or self.dedicated_account_bank
        if chosen:
            return chosen
        if self.secret_key.startswith("sk_test_"):
            return self.TEST_PREFERRED_BANK
        return self.LIVE_DEFAULT_PREFERRED_BANK

    def create_virtual_account(self, *, reference, customer_name, customer_email="",
                               preferred_bank="", metadata=None, subaccount=""):
        # Paystack requires a Customer first, then a dedicated account against it.  # Two-step account setup.
        first, _, last = (customer_name or "Customer").partition(" ")  # Split the display name into first/last names.
        customer = self._require_ok(self._post("/customer", {  # Create the upstream Paystack customer.
            "email": customer_email or f"{reference}@example.com",  # Fall back to a deterministic placeholder email.
            "first_name": first, "last_name": last or first,  # Use the available name parts.
        }))
        body = {
            "customer": customer.get("customer_code", ""),
            "preferred_bank": self.preferred_bank_slug(preferred_bank),
        }
        if subaccount:  # Deposits settle to the branch's bank, the branch bearing the fee.
            body["subaccount"] = subaccount
            body["bearer"] = "subaccount"
        data = self._require_ok(self._post("/dedicated_account", body))  # Request the dedicated account.
        acct = data.get("dedicated_account", data)
        bank = acct.get("bank", {}) if isinstance(acct.get("bank"), dict) else {}
        return VirtualAccountResult(
            account_number=acct.get("account_number", ""),
            bank_name=bank.get("name", ""),
            account_name=acct.get("account_name", customer_name),
            provider_reference=str(acct.get("id", "")),
            raw=data,  # Keep the raw response for audit/debugging.
        )

    def verify_collection(self, *, reference, provider_reference=""):
        data = self._require_ok(self._get(f"/transaction/verify/{reference}"))  # Ask Paystack for the final state.
        gateway = (data.get("status") or "").lower()
        return CollectionStatusResult(
            reference=reference,  # Merchant reference passed back for correlation.
            provider_reference=str(data.get("id", provider_reference)),
            status=_COLLECTION_STATUS.get(gateway, "PROCESSING"),
            amount=int(data.get("amount", 0) or 0),
            currency=data.get("currency", "NGN"),
            paid_at=_instant(data.get("paid_at") or data.get("paidAt")),
            fee=_kobo_or_none(data.get("fees")),
            raw=data,  # Keep the raw response payload.
        )

    # -- refunds ------------------------------------------------------------ #
    def create_refund(self, *, transaction_reference, amount, currency="NGN",
                      customer_note="", merchant_note=""):
        """Ask Paystack to return ``amount`` kobo of a payment (``POST /refund``).

        Paystack facts this relies on, from its Refund API reference
        (docs-v2.production.paystack.co/docs/api/refund): ``transaction`` is the
        payment's reference or id (required), ``amount`` is in the currency's
        subunit and defaults to the whole payment, ``currency``, ``customer_note``
        and ``merchant_note`` are optional; the reply carries ``id``, ``status``,
        ``amount``, ``currency`` and ``transaction`` (``id``, ``reference``).
        Paystack does not return its own fee on the original payment when it
        refunds it, so nothing is booked for that fee: it stays the tenant's bank
        charge. There is no merchant reference parameter, so a refund is found
        again by its ``id`` or by its payment.
        """
        body = {"transaction": transaction_reference, "amount": int(amount), "currency": currency}
        if customer_note:
            body["customer_note"] = customer_note[:255]
        if merchant_note:
            body["merchant_note"] = merchant_note[:255]
        data = self._require_ok(self._post("/refund", body))
        return _refund_result(data, transaction_reference)

    def verify_refund(self, *, provider_reference="", transaction_reference="", amount=0):
        """The current answer for a refund (``GET /refund/<id>``).

        With no id, the payment's refunds are listed (``GET /refund?transaction=``)
        and the newest of ``amount`` kobo, or the newest of any amount when none
        is given, answers; none found reads as still processing.
        """
        if provider_reference:
            data = self._require_ok(self._get(f"/refund/{provider_reference}"))
            return _refund_result(data, transaction_reference)
        rows = self._require_ok(self._get(f"/refund?transaction={transaction_reference}"))
        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        if amount:
            rows = [row for row in rows if _kobo_or_none(row.get("amount")) == int(amount)]
        if not rows:
            return RefundResult(transaction_reference=transaction_reference,
                                status="PROCESSING", provider_status="pending")
        newest = max(rows, key=lambda row: _kobo_or_none(row.get("id")) or 0)
        return _refund_result(newest, transaction_reference)

    # -- payment terminals ------------------------------------------------- #
    def create_terminal(self, *, name, destinations, currency="NGN", metadata=None):
        body = {"name": name, "destinations": list(destinations or []), "currency": currency}
        if metadata:
            body["metadata"] = json.dumps(metadata)
        return _terminal_result(self._require_ok(self._post("/virtual_terminal", body)))

    def fetch_terminal(self, *, code):
        return _terminal_result(self._require_ok(self._get(f"/virtual_terminal/{code}")))

    def deactivate_terminal(self, *, code):
        self._require_ok(self._put(f"/virtual_terminal/{code}/deactivate"))

    def assign_terminal_destinations(self, *, code, destinations):
        self._require_ok(self._post(f"/virtual_terminal/{code}/destination/assign",
                                    {"destinations": list(destinations)}))

    def unassign_terminal_destinations(self, *, code, targets):
        self._require_ok(self._post(f"/virtual_terminal/{code}/destination/unassign",
                                    {"targets": list(targets)}))

    def create_split(self, *, name, subaccount_code, currency="NGN"):
        data = self._require_ok(self._post("/split", {
            "name": name, "type": "percentage", "currency": currency,
            "subaccounts": [{"subaccount": subaccount_code, "share": 100}],
            "bearer_type": "subaccount", "bearer_subaccount": subaccount_code,
        }))
        return SplitResult(split_code=str((data or {}).get("split_code", "") or ""), raw=data)

    def attach_terminal_split(self, *, code, split_code):
        self._require_ok(self._put(f"/virtual_terminal/{code}/split_code",
                                   {"split_code": split_code}))

    def detach_terminal_split(self, *, code, split_code):
        self._require_ok(self._delete(f"/virtual_terminal/{code}/split_code",
                                      {"split_code": split_code}))

    # -- settlement subaccounts ------------------------------------------- #
    def create_subaccount(self, *, business_name, settlement_bank_code, account_number,
                          percentage_charge=0):
        data = self._require_ok(self._post("/subaccount", {
            "business_name": business_name,
            "bank_code": settlement_bank_code,
            "settlement_bank": settlement_bank_code,
            "account_number": account_number,
            "percentage_charge": percentage_charge,
        }))
        return _subaccount_result(data)

    def update_subaccount(self, *, subaccount_code, business_name, settlement_bank_code,
                          account_number):
        data = self._require_ok(self._put(f"/subaccount/{subaccount_code}", {
            "business_name": business_name,
            "bank_code": settlement_bank_code,
            "settlement_bank": settlement_bank_code,
            "account_number": account_number,
        }))
        result = _subaccount_result(data)
        return result if result.subaccount_code else SubaccountResult(
            subaccount_code=subaccount_code, account_name=result.account_name, raw=data)

    # -- payout ------------------------------------------------------------- #  # Payout-side operations.
    # Handle the create transfer workflow.
    def create_transfer(self, *, reference, amount, currency, account_number, bank_code,
                        account_name="", narration="", metadata=None):
        recipient = self._require_ok(self._post("/transferrecipient", {  # Create or resolve the transfer recipient.
            "type": "nuban", "name": account_name or "Beneficiary",  # Paystack expects a recipient type and name.
            "account_number": account_number, "bank_code": bank_code, "currency": currency,  # Bank details for the payee.
        }))
        recipient_code = recipient.get("recipient_code", "")
        data = self._require_ok(self._post("/transfer", {  # Initiate the actual bank transfer.
            "source": "balance", "amount": amount, "recipient": recipient_code,  # Pull from the wallet balance.
            "reason": narration or "Payout", "reference": reference, "currency": currency,  # Attach bookkeeping fields.
        }))
        status = (data.get("status") or "").lower()
        return TransferResult(
            reference=reference,  # Merchant reference for the transfer.
            provider_reference=data.get("transfer_code", ""),
            status=_TRANSFER_STATUS.get(status, "PROCESSING"),
            recipient_code=recipient_code,  # Save the recipient token for later verification.
            raw=data,  # Preserve the raw response.
        )

    def resolve_account(self, *, account_number, bank_code):
        """Who holds ``account_number`` at ``bank_code`` (``GET /bank/resolve``).

        Paystack answers ``data.account_name`` for an account it can resolve,
        and an HTTP 4xx (surfaced as :class:`ProviderError`) for one it cannot.
        Confirmed against Paystack's Verification API reference on 2026-10-13:
        the ``account_number`` and ``bank_code`` query parameters and the
        ``data.account_name`` field. The reference documents no error codes or
        rate limits for this endpoint.
        """
        query = urllib.parse.urlencode({"account_number": account_number, "bank_code": bank_code})
        data = self._require_ok(self._get(f"/bank/resolve?{query}"))
        name = str((data or {}).get("account_name") or "").strip()
        if not name:
            raise ProviderError("Paystack did not name the holder of this account.",
                                provider=self.name)
        return AccountResolution(account_number=account_number, account_name=name, raw=data)

    #: Pages of ``GET /bank`` read before the listing is refused as unbounded.
    BANK_MAX_PAGES = 10

    def list_banks(self):
        """Nigeria's active banks in naira (``GET /bank``), every page, by name.

        Reads with cursor pagination (``use_cursor=true``, ``perPage=100``, the
        next page named by ``meta.next``). Paystack's Miscellaneous reference
        documents ``country``, ``currency``, ``perPage``, ``use_cursor`` and the
        ``meta.next`` cursor; that the cursor is sent back as ``next`` is the
        reference's convention and has not been exercised against the live API.
        """
        banks, cursor = {}, ""
        for _page in range(self.BANK_MAX_PAGES):
            query = {"country": "nigeria", "currency": "NGN", "perPage": 100, "use_cursor": "true"}
            if cursor:
                query["next"] = cursor
            response = self._get(f"/bank?{urllib.parse.urlencode(query)}")
            data = self._require_ok(response)
            for row in data if isinstance(data, list) else []:
                if isinstance(row, dict) and row.get("code") and row.get("active", True):
                    banks[str(row["code"])] = str(row.get("name") or row["code"])
            meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
            cursor = str(meta.get("next") or "")
            if not cursor:
                break
        return sorted(({"name": name, "code": code} for code, name in banks.items()),
                      key=lambda row: row["name"].lower())

    #: Paystack's transfer fee by amount sent, in kobo, as on paystack.com/pricing:
    #: N10 for N5,000 and below, N25 from N5,001 to N50,000, N50 above.
    TRANSFER_FEE_TIERS = ((500_000, 1_000), (5_000_000, 2_500), (None, 5_000))

    def transfer_fee(self, amount: int) -> int:
        """Paystack's fee for a transfer of ``amount`` kobo, from :attr:`TRANSFER_FEE_TIERS`."""
        for ceiling, fee in self.TRANSFER_FEE_TIERS:
            if ceiling is None or int(amount) <= ceiling:
                return fee
        return 0

    def available_balance(self, currency="NGN"):
        """The merchant's Paystack balance in ``currency``, in kobo (``GET /balance``).

        Paystack answers a list of ``{currency, balance}`` rows in the currency's
        subunit (Transfer Control API reference); a currency it does not list
        holds nothing.
        """
        data = self._require_ok(self._get("/balance"))
        for row in data if isinstance(data, list) else []:
            if str(row.get("currency", "")).upper() == currency.upper():
                return int(row.get("balance") or 0)
        return 0

    #: Rows asked for per page of ``GET /settlement``.
    SETTLEMENT_PAGE_SIZE = 100
    #: Pages read before the listing is refused as unbounded.
    SETTLEMENT_MAX_PAGES = 50

    def list_settlements(self, *, start, end):
        """Paystack's settlements of the main account's balance to its bank (``GET /settlement``).

        Reads every page from ``start`` to ``end`` (dates, sent as ``from`` and
        ``to``) for the main account only (``subaccount=none``). Each settlement
        is translated by :func:`_settlement_record`. A listing that runs past
        :attr:`SETTLEMENT_MAX_PAGES` raises :class:`ProviderError` rather than
        answering part of the list, since a settlement left unread would show
        as a mismatch.

        Paystack facts this relies on, each confirmed against Paystack's
        Settlement API reference (paystack.com/docs/api/settlement): the
        ``from``, ``to``, ``perPage``, ``page`` and ``subaccount=none``
        parameters (``none`` lists the main account only); ``meta.pageCount``
        on each page; the statuses ``success``, ``processing``, ``pending`` and
        ``failed``; and the row fields read by :func:`_settlement_record`, with
        ``settlement_date`` in UTC. The reference does not say whether a
        ``processing`` settlement has already left the balance, which is why the
        daily check records a gap fully explained by one as not measured.
        """
        rows, page = [], 1
        while True:
            if page > self.SETTLEMENT_MAX_PAGES:
                from vs_config.display import format_date

                raise ProviderError(
                    f"Paystack listed more than {self.SETTLEMENT_MAX_PAGES} pages of settlements "
                    f"from {format_date(start, None)} to {format_date(end, None)}.",
                    provider=self.name)
            response = self._get(
                f"/settlement?perPage={self.SETTLEMENT_PAGE_SIZE}&page={page}"
                f"&from={start.isoformat()}&to={end.isoformat()}&subaccount=none")
            data = self._require_ok(response)
            data = data if isinstance(data, list) else []
            rows.extend(_settlement_record(row) for row in data if isinstance(row, dict))
            meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
            page_count = _kobo_or_none(meta.get("pageCount"))
            if page_count is not None:
                if page >= page_count:
                    return rows
            elif len(data) < self.SETTLEMENT_PAGE_SIZE:
                return rows
            page += 1

    def verify_transfer(self, *, reference, provider_reference=""):
        data = self._require_ok(self._get(f"/transfer/verify/{reference}"))  # Re-query the final transfer state.
        status = (data.get("status") or "").lower()
        return TransferResult(
            reference=reference,  # Merchant reference for the transfer.
            provider_reference=data.get("transfer_code", provider_reference),
            status=_TRANSFER_STATUS.get(status, "PROCESSING"),
            amount=int(data.get("amount") or 0),  # Paystack transfer verify returns the kobo amount in data.amount.
            failure_reason=data.get("message", "") if status in ("failed", "reversed") else "",
            paid_at=_instant(data.get("transferred_at")) if status == "success" else None,
            raw=data,  # Keep the raw provider payload.
        )

    # -- webhooks ----------------------------------------------------------- #  # Webhook verification and parsing.
    # Handle the verify signature workflow.
    def verify_signature(self, *, raw_body: bytes, headers: dict) -> bool:
        sent = _header(headers, "x-paystack-signature")  # Read the signature supplied by Paystack.
        if not sent:  # Missing signatures are invalid.
            return False
        expected = hmac.new(self.secret_key.encode(), raw_body, hashlib.sha512).hexdigest()  # Compute the expected HMAC.
        return hmac.compare_digest(sent, expected)  # Compare in constant time.

    # Handle the parse webhook workflow.
    def parse_webhook(self, *, payload, raw_body, headers):
        """The neutral view of a Paystack event.

        ``transfer.*`` events concern payouts. ``charge.dispute.*`` (a chargeback)
        and ``refund.*`` (a refund made at Paystack) concern an earlier payment,
        which they name inside ``data.transaction`` or as
        ``data.transaction_reference`` *(confirm both shapes)*. Everything else
        is a collection event.
        """
        event = payload.get("event", "")
        data = payload.get("data", {})
        if event.startswith("charge.dispute") or event.startswith("refund"):
            status = str(data.get("status", "") or "")
            if event == "charge.dispute.resolve":
                status = _dispute_outcome(data) or status
            transaction_block = data.get("transaction") or {}
            if not isinstance(transaction_block, dict):
                transaction_block = {}
            reference = (transaction_block.get("reference")
                         or data.get("transaction_reference") or "")
            return WebhookParseResult(
                event_type=event,
                direction="DISPUTE" if event.startswith("charge.dispute") else "REFUND",
                reference=reference,
                provider_reference=str(transaction_block.get("id", "") or ""),
                status=status,
                amount=int(data.get("refund_amount") or data.get("amount")
                           or transaction_block.get("amount") or 0),
                currency=data.get("currency", "NGN"),
                dedupe_key=_dedupe_key(
                    event, data.get("id"), data.get("refund_reference"), reference,
                    raw_body=raw_body),
                destination_account_number="",
                raw=payload,
            )
        if event.startswith("transfer"):  # Transfer events correspond to outbound payouts.
            gateway = (data.get("status") or "").lower()
            status = _TRANSFER_STATUS.get(gateway, "PROCESSING")
            if event == "transfer.success":  # Paystack emits a definitive success event.
                status = "PAID"  # Explicitly mark the payout as paid.
            elif event == "transfer.failed":  # Terminal failure event.
                status = "FAILED"  # Mark the transfer failed.
            elif event == "transfer.reversed":  # Terminal reversal event.
                status = "REVERSED"  # Mark the transfer reversed.
            direction = "PAYOUT"  # Route transfer events to the payout confirm path.
        else:  # All remaining events are treated as collection-side events.
            status = (
                "SUCCEEDED" if event == "charge.success"  # charge.success is the canonical successful collection event.
                else _COLLECTION_STATUS.get((data.get("status") or "").lower(), "PROCESSING")
            )
            direction = "COLLECTION"  # Route non-transfer events to the collection confirm path.
        reference = data.get("reference", "")
        terminal = _terminal_payment_facts(data) if direction == "COLLECTION" else {}
        return WebhookParseResult(
            event_type=event,  # Preserve the provider event name.
            direction=direction,  # COLLECTION or PAYOUT.
            **terminal,
            reference=reference,  # Merchant reference for matching local records.
            provider_reference=str(data.get("id", "")),
            status=status,  # Neutral status for the downstream confirm flow.
            amount=int(data.get("amount", 0) or 0),
            currency=data.get("currency", "NGN"),
            dedupe_key=_dedupe_key(event, reference, data.get("id"), raw_body=raw_body),
            destination_account_number=(
                _dedicated_nuban(data) if direction == "COLLECTION" else ""
            ),
            raw=payload,  # Keep the original normalized payload.
        )


def _dedupe_key(event: str, *identifiers, raw_body: bytes = b"") -> str:
    """The idempotency key of one event: its type and the first stable id it carries.

    Events about a payment or transfer carry our reference or Paystack's id.
    Others (``dedicatedaccount.assign.*``, ``customeridentification.*``) may
    carry neither at the top level, and a key ending in nothing would make a
    second, different event of the same type look like a re-delivery. The last
    resort is a hash of the raw body: a genuine re-delivery repeats the body, a
    different event does not.
    """
    for identifier in identifiers:
        if identifier not in (None, ""):
            return f"PAYSTACK:{event}:{identifier}"
    return f"PAYSTACK:{event}:{hashlib.sha256(raw_body or b'').hexdigest()}"


def _terminal_result(data) -> TerminalResult:
    """The neutral view of a Paystack virtual terminal response body.

    ``details`` keeps what the response says about how to pay at the terminal
    (``paymentMethods`` and any payment page, QR or link fields), under the
    names Paystack used, since the reference names only ``paymentMethods``.
    """
    data = data if isinstance(data, dict) else {}
    kept = ("paymentMethods", "payment_methods", "payment_url", "payment_link", "link",
            "qr_code", "qr_code_url", "poster_url", "destinations", "currency")
    return TerminalResult(
        code=str(data.get("code", "") or ""),
        name=str(data.get("name", "") or ""),
        active=bool(data.get("active", True)),
        details={key: data[key] for key in kept if key in data},
        raw=data,
    )


def _terminal_payment_facts(data: dict) -> dict:
    """The terminal, payer and sender facts a ``charge.success`` event carries, as parse-result fields.

    The terminal is ``data.metadata.virtual_terminal.code``; ``metadata`` may be a
    JSON string. The payer's email is ``data.customer.email``, and the sender is
    ``data.authorization.sender_name`` (a bank transfer) or ``account_name`` (a card
    holder), with ``sender_bank`` or ``bank``. Every field is empty for an event
    that did not come from a terminal, which is the only one that uses them.
    """
    meta = data.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    terminal = meta.get("virtual_terminal") if isinstance(meta, dict) else None
    code = terminal.get("code") if isinstance(terminal, dict) else terminal
    if not isinstance(code, str) or not code.strip():
        return {}
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    auth = data.get("authorization") if isinstance(data.get("authorization"), dict) else {}
    return {
        "terminal_code": code.strip(),
        "payer_email": str(customer.get("email", "") or "").strip(),
        "sender_name": str(auth.get("sender_name") or auth.get("account_name") or "").strip(),
        "sender_bank": str(auth.get("sender_bank") or auth.get("bank") or "").strip(),
    }


#: Paystack's ``data.resolution`` on ``charge.dispute.resolve``. ``declined`` and
#: ``merchant-accepted`` are in the Dispute API reference; ``auto-accepted`` (a
#: dispute nobody answered in time) is not, and is read as lost to be safe:
#: a dispute ``declined`` went the merchant's way; one the merchant accepted, or
#: that was accepted for it, went the payer's.
_DISPUTE_RESOLUTIONS = {
    "declined": "WON",
    "merchant-accepted": "LOST",
    "auto-accepted": "LOST",
}


def _dispute_outcome(data: dict) -> str:
    """The neutral outcome a dispute resolution reports, or "" when it names none."""
    return _DISPUTE_RESOLUTIONS.get(str(data.get("resolution", "") or "").strip().lower(), "")


def _refund_result(data, transaction_reference="") -> RefundResult:
    """The neutral view of one Paystack refund object."""
    data = data if isinstance(data, dict) else {}
    transaction = data.get("transaction")
    if isinstance(transaction, dict):
        reference = transaction.get("reference") or transaction_reference
    else:
        reference = str(transaction or "") or transaction_reference
    status = str(data.get("status", "") or "").strip().lower()
    return RefundResult(
        transaction_reference=str(reference or ""),
        provider_reference=str(data.get("id", "") or ""),
        status=_REFUND_STATUS.get(status, "PROCESSING"),
        provider_status=status,
        amount=_kobo_or_none(data.get("amount")) or 0,
        currency=str(data.get("currency", "") or "NGN"),
        failure_reason=str(data.get("message", "") or "") if status == "failed" else "",
        raw=data,
    )


def _kobo_or_none(value):
    """An integer kobo figure from a Paystack field, or None when absent or unreadable."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: Paystack's settlement ``status`` (Settlement API reference: success, processing,
#: pending, failed) mapped to the neutral one. Only
#: ``SETTLED`` is money that has left the balance for the bank.
_SETTLEMENT_STATUS = {
    "success": "SETTLED",
    "settled": "SETTLED",
    "pending": "PENDING",
    "processing": "PENDING",
    "failed": "FAILED",
}


def _settlement_record(row: dict) -> SettlementRecord:
    """The neutral view of one Paystack settlement row.

    The amount is ``effective_amount`` (what reached the bank, after any
    deductions), or ``total_amount`` when Paystack sends no effective figure;
    the instant is ``settlement_date``; a subaccount settlement names it in
    ``subaccount`` as an object or a code. The fields are as in Paystack's
    Settlement API reference; the listing asks for the main account only
    (``subaccount=none``), so the subaccount shape is a safeguard.
    """
    amount = _kobo_or_none(row.get("effective_amount"))
    if amount is None:
        amount = _kobo_or_none(row.get("total_amount"))
    subaccount = row.get("subaccount")
    if isinstance(subaccount, dict):
        subaccount = subaccount.get("subaccount_code") or subaccount.get("id") or ""
    return SettlementRecord(
        settlement_id=str(row.get("id", "") or ""),
        status=_SETTLEMENT_STATUS.get(str(row.get("status", "") or "").strip().lower(), "UNKNOWN"),
        currency=str(row.get("currency", "") or "").strip().upper(),
        amount=amount,
        settled_at=_instant(row.get("settlement_date") or row.get("settled_at")),
        subaccount=str(subaccount or ""),
        raw=row,
    )


def _subaccount_result(data: dict) -> SubaccountResult:
    """The neutral result for a Paystack subaccount response body."""
    data = data if isinstance(data, dict) else {}
    return SubaccountResult(
        subaccount_code=str(data.get("subaccount_code", "") or ""),
        account_name=str(data.get("account_name", "") or ""),
        raw=data,
    )


# Support the dedicated nuban workflow.
def _dedicated_nuban(data: dict) -> str:
    """Return the dedicated virtual account a ``charge.success`` was paid into, if any.

    Paystack reports a transfer into a dedicated account as an ordinary ``charge.success``
    whose channel is ``dedicated_nuban``. The receiving NUBAN is not a top-level field: it
    rides on ``data.authorization.receiver_bank_account_number``, and older/alternate
    payloads repeat it as ``data.metadata.receiver_account_number``. We read both and
    return ``""`` for every other collection event (card, USSD, hosted checkout), so a
    normal charge is never mistaken for a virtual-account deposit.
    """
    auth = data.get("authorization") or {}  # Authorization block, when the event carries one.
    if not isinstance(auth, dict):  # Defend against a provider sending a non-object here.
        auth = {}
    meta = data.get("metadata") or {}  # Paystack sometimes echoes the receiver in metadata.
    if not isinstance(meta, dict):  # Paystack allows metadata to be a bare string.
        meta = {}
    number = (
        auth.get("receiver_bank_account_number")
        or meta.get("receiver_account_number")
        or ""
    )
    return str(number).strip()  # Normalize to a bare string for the local lookup.


def _instant(value):
    """An aware datetime from a Paystack timestamp, or ``None`` when absent or unreadable.

    Paystack reports ISO-8601 instants in UTC (``2026-08-31T20:40:11.000Z``). A value
    without an offset is read as UTC, which is what Paystack means by it. Unreadable
    input yields ``None`` rather than raising: the date is a refinement of the
    booking, and a receipt must never fail to book because a timestamp was odd.
    """
    if not value:
        return None
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    try:
        parsed = parse_datetime(str(value))
    except ValueError:
        return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime.timezone.utc)
    return parsed


# Support the header workflow.
def _header(headers: dict, name: str) -> str:
    """Case-insensitive header lookup (WSGI/DRF may upper/lower-case keys)."""
    if not headers:  # Missing headers should behave like an empty mapping.
        return ""
    name = name.lower()  # Normalize the lookup key once.
    for key, value in headers.items():  # Walk through the received headers.
        if key.lower() == name:  # Match case-insensitively.
            return value  # Return the original header value.
    return ""  # No matching header was found.
