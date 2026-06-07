"""Provider implementations for vs_payments.

The ledger never sees a provider directly — everything goes through the abstract
:mod:`~vs_payments.providers.base` interface, resolved by
:func:`~vs_payments.providers.registry.get_provider`. Swapping OPay for Paystack (or a
fake in tests) is a config change, not a code change.
"""
