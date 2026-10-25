"""How the books' owner wants a customer to pay, for pay links, emails and invoices.

The finance engine can send a customer to a hosted card checkout from a public
pay link, print a "pay online" block and QR code on an invoice, and email the
link. Whether that is how a given customer should pay is not the engine's
decision: a school may ask its families to pay by bank transfer to the
account number the bank issued for the pupil, and keep card for the desk. A
product hosting the books registers a provider here and answers per customer;
the engine never imports the product.

A provider is called as ``provider(customer) -> PayerPaymentOptions | None``.
``None`` means the provider has no opinion about this customer, and the engine
behaves as it always has: the pay link starts a checkout, the invoice prints
the pay-online block, the email carries the link. With no provider registered
every customer is in that state.

When the answer is ``card_allowed=False``:

* the public pay link still shows the balance but refuses to start a checkout
  (:class:`vs_finance.pay_links.CardPaymentsOffError`);
* the invoice replaces its pay-online block and QR code with the customer's
  transfer account, or with the provider's sentence when none exists yet;
* the invoice email replaces its pay-online line and button the same way;
* once the customer has a transfer account, the invoice (document, PDF and the
  PDF an email carries) and the customer's own screen leave out the issuer's
  own bank block, so the customer pays only into the account that is matched
  to the invoice automatically (:func:`issuer_bank_withheld`). A customer with
  no account yet keeps the issuer's bank block, since it is the only account
  they can pay into.

The account number is a switched field of the payments engine
(``payments.virtual_account.account_number``). Documents addressed to the payer
carry it as the issuer's bank carries its own, so the answer is a declared system
surface (:data:`vs_rbac.field_enforcement.SYSTEM_SURFACES`); the staff screen that
renders an invoice for its own reader filters it by that reader.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The line printed under a transfer account, on the invoice and in the email.
TRANSFER_MATCHED_LINE = "Transfers to this account are matched to the invoice automatically."


@dataclass(frozen=True)
class PayerTransferAccount:
    """A bank account a customer pays into: where, which number and whose name."""

    bank_name: str
    account_number: str
    account_name: str


@dataclass(frozen=True)
class PayerPaymentOptions:
    """What the books' owner says about how one customer pays.

    ``card_allowed`` False sends the customer to ``transfer_account`` instead.
    ``card_off_message`` is what the pay page says when it refuses a checkout;
    ``no_account_message`` is what an invoice or email says in place of the
    account while the customer has none. Both are the owner's own words, so
    this module names no product.
    """

    card_allowed: bool = True
    transfer_account: PayerTransferAccount | None = None
    card_off_message: str = ""
    no_account_message: str = ""


def shows_payer_account(options: PayerPaymentOptions | None) -> bool:
    """Whether *options* sends the customer to a transfer account they already have."""
    return options is not None and not options.card_allowed and options.transfer_account is not None


_PROVIDERS: list = []


def register_payer_options_provider(provider) -> None:
    """Add *provider*, called as ``provider(customer) -> PayerPaymentOptions | None``. Idempotent."""
    if provider not in _PROVIDERS:
        _PROVIDERS.append(provider)


def payer_payment_options(customer) -> PayerPaymentOptions | None:
    """The first provider answer for *customer*, or ``None`` for the engine's own behaviour.

    A provider that fails is logged and passed over: a customer is never kept
    from paying, or from receiving their invoice, because a provider broke.
    """
    from vs_rbac.field_enforcement import assert_system_surface

    assert_system_surface("vs_finance.payer_options.payer_payment_options")
    for provider in _PROVIDERS:
        try:
            found = provider(customer)
        except Exception:
            logger.exception("A payer options provider failed for customer %s.", customer.pk)
            continue
        if found is not None:
            return found
    return None


def card_payments_allowed(customer) -> bool:
    """Whether *customer* may be sent to a hosted card checkout."""
    options = payer_payment_options(customer)
    return options is None or options.card_allowed


def issuer_bank_withheld(customer) -> bool:
    """Whether a document for *customer* leaves out the issuer's own bank block.

    True exactly when the customer is shown a transfer account of their own
    (:func:`shows_payer_account`). Every document and screen that would print
    the issuer's collection account asks this, so they agree.
    """
    return shows_payer_account(payer_payment_options(customer))


def transfer_instruction(options: PayerPaymentOptions) -> str:
    """One plain line telling a customer where to pay by transfer, for an email body.

    The account when the customer has one, with the line that transfers are
    matched automatically; otherwise the owner's sentence for a customer with
    none yet.
    """
    account = options.transfer_account
    if account is None:
        return options.no_account_message
    return (
        f"Pay by bank transfer to {account.bank_name}, account number "
        f"{account.account_number}, account name {account.account_name}. "
        f"{TRANSFER_MATCHED_LINE}"
    )


def email_payment_context(invoice, pay_url: str) -> dict:
    """The ``payment_link`` and ``payment_instructions`` an invoice email prints.

    Where card is allowed (or nobody has an opinion) the link is *pay_url* and
    the instruction is the "Pay online" line the email always carried. Where
    it is not, the link is empty, so the email carries no button, and the
    instruction says how to pay by transfer.
    """
    options = payer_payment_options(invoice.customer)
    if options is None or options.card_allowed:
        return {
            "payment_link": pay_url,
            "payment_instructions": f"Pay online: {pay_url}" if pay_url else "",
        }
    return {"payment_link": "", "payment_instructions": transfer_instruction(options)}
