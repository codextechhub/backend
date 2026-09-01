"""The FAL's routes.

Deliberately narrow. This exposes the two operations that turn a school's
academic structure into money owed - linking a fee structure to a term, and
billing a named cohort from it - and nothing else. Reading invoices, taking
payment and granting relief all happen on ``vs_finance``'s own surface, which is
domain-neutral and already routed; putting a second door in front of them here
would duplicate the engine rather than bridge to it.

The nine procurement actions the FAL also exposes are absent on purpose: they
belong to the procurement bridge, which is its own piece of work.
"""
from django.urls import path

from .views import GenerateInvoicesView, LinkTermView

urlpatterns = [
    path(
        "fee-structures/<int:pk>/link-term/",
        LinkTermView.as_view(),
        name="fal-fee-structure-link-term",
    ),
    path(
        "fee-structures/<int:pk>/generate-invoices/",
        GenerateInvoicesView.as_view(),
        name="fal-fee-structure-generate-invoices",
    ),
]
