"""Signals the payments engine sends about its own records.

Another app reacts to a payments event through a signal here rather than by
being imported into the engine: ``vs_payments`` names no listener and knows
nothing of what a listener does with the news.

``virtual_account_replaced`` (``old``, ``new``) is sent once a customer's
dedicated account has been switched off for good and another issued in its
place, or none (``new`` is None) when the provider refused one. It is sent
after the change has committed, never from inside it, and robustly: a
listener that raises is logged and the others still run.

``virtual_account_deposit_booked`` (``intent``) is sent once a transfer into a
customer's dedicated account has been confirmed and its receipt booked
(``intent.payment`` is the receipt). A payment someone started in an app is
told to the person who started it by the engine itself; a deposit has no such
person, so whoever hosts the books decides who is told. It is sent after the
booking committed, once per deposit, and robustly like the signal above.
"""
import django.dispatch

virtual_account_replaced = django.dispatch.Signal()
virtual_account_deposit_booked = django.dispatch.Signal()
