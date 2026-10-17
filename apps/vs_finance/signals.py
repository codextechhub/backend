"""Signals the finance engine sends about its own documents.

Another app reacts to a finance event through a signal here rather than by
being imported into the engine: ``vs_finance`` names no listener and knows
nothing of what a listener does with the news.

``invoice_posted`` (``invoice``) is sent once an invoice's posting has
committed, never from inside the posting transaction, so a listener cannot
roll a bill back or hold its transaction open, and a posting that rolls back
(a cohort run's preview, a refused batch) sends nothing. Each listener is run
robustly: one that raises is logged and the others still run, and the bill
stays posted.
"""
import django.dispatch

invoice_posted = django.dispatch.Signal()
