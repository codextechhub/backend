"""Signals the payments engine sends about its own records.

Another app reacts to a payments event through a signal here rather than by
being imported into the engine: ``vs_payments`` names no listener and knows
nothing of what a listener does with the news.

``virtual_account_replaced`` (``old``, ``new``) is sent once a customer's
dedicated account has been switched off for good and another issued in its
place, or none (``new`` is None) when the provider refused one. It is sent
after the change has committed, never from inside it, and robustly: a
listener that raises is logged and the others still run.
"""
import django.dispatch

virtual_account_replaced = django.dispatch.Signal()
