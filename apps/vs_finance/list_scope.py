"""The branch and the stretch of time a finance document list, or a summary of one, answers for.

Every finance document list reads two request filters on top of the caller's
own reach:

* ``?branch=<id>`` keeps the documents of one branch the caller works in, and
  ``?branch=unassigned`` the ones not yet given a branch
  (:func:`vs_finance.views_ops.base._filter_by_branch`, the rule every
  receivables list already reads); and
* ``?fiscal_year=`` or ``?date_from=`` / ``?date_to=`` keeps the documents
  dated in that year or range, each placed by its own date
  (:mod:`vs_finance.period_filter`).

Both are ANDed, so Mrs Bello asking for Ikeja and FY2015 sees Ikeja's 2015
invoices and only those, and the figures above the list count the same rows.

The three places every finance document list is paginated apply this once
(``_FinanceBase.paginate``, ``EntityScopedListMixin`` and the receivables
``_paginate``), and each summary applies it to the rows it counts, so a list
cannot read the filters differently from the figures above it.

A list whose ``?branch=`` means something else declares ``filters_own_branch``
and keeps its own reading: an inter-branch transfer is in the list of the
branch it leaves and of the branch it reaches, a payer payment received at
Ikeja that carries Lekki's share is in Lekki's list too, and a provision run is
in the list of every branch its lines name. The period still applies to them.

Only finance documents are narrowed. A list of anything else passes through
unchanged.
"""
from __future__ import annotations

from django.db.models import QuerySet

from .period_filter import narrow_document_list, period_q


def _is_branch_document(queryset) -> bool:
    from .models.core import FinanceDocument

    if not isinstance(queryset, QuerySet) or not issubclass(queryset.model, FinanceDocument):
        return False
    return any(f.name == "branch" for f in queryset.model._meta.concrete_fields)


def narrow_finance_document_list(queryset, request, view=None, *, entity=None, archived=True):
    """``queryset`` narrowed to the caller's ``?branch=`` and period, archived years handled.

    ``view`` is the list view, read for ``filters_own_branch``; a summary passes
    none. ``entity`` saves resolving it again where the caller holds it.
    ``archived=False`` applies the branch and a named period and leaves the
    archived years as they are, for a register's figures (every asset still in
    use, every filing still owed) that count whole books when nothing is picked.
    """
    params = getattr(request, "query_params", None) or getattr(request, "GET", {})
    if (
        params.get("branch")
        and not getattr(view, "filters_own_branch", False)
        and _is_branch_document(queryset)
    ):
        from .views import resolve_entity
        from .views_ops.base import _filter_by_branch

        queryset = _filter_by_branch(queryset, request, entity or resolve_entity(request))
    if not archived:
        from .views import resolve_entity

        return queryset.filter(period_q(request, entity or resolve_entity(request), queryset.model))
    return narrow_document_list(queryset, request)
