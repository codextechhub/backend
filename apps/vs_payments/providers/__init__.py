"""Provider implementations for vs_payments.

The ledger never sees a provider directly — everything goes through the abstract
:mod:`~vs_payments.providers.base` interface, resolved by
:func:`~vs_payments.providers.registry.get_provider`. Swapping Paystack for a
fake in tests (or a future provider) is a config change, not a code change.
"""
