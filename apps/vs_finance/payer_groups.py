"""How the books' owner groups its payers, for "collection by group" figures.

A school reads its collections by class ("JSS1 is at 82%, SS3 at 61%"); finance
cannot, because it does not know what a class is and serves books that have
none. So the owner's layer answers: a provider named by dotted path in the
``FINANCE_PAYER_GROUP_PROVIDER`` setting is called as
``provider(entity, customer_ids) -> PayerGrouping | None``. The school layer's
answers with each child's current class. With no provider, or ``None`` for
these books, there is no grouping and the card is left out.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string


@dataclass(frozen=True)
class PayerGrouping:
    """``groups`` maps a customer id to its group's label; ``label`` names the kind ("Class")."""

    label: str
    groups: dict


@lru_cache(maxsize=1)
def _provider(path: str):
    return import_string(path)


def group_payers(entity, customer_ids) -> PayerGrouping | None:
    """The owner's grouping of ``customer_ids``, or ``None`` when it has none."""
    path = getattr(settings, "FINANCE_PAYER_GROUP_PROVIDER", "")
    if not path:
        return None
    return _provider(path)(entity, list(customer_ids))
