"""How finance names its periods, branches and states to a person.

Every sentence a finance, procurement or payments refusal shows is read by a
bursar, not a developer, so it is written from the words on their screens:
"September 2026", "Ikeja", "pending approval". Never from what the code holds:
a model's ``__str__`` (``2026-09 [CLOSED]``), a stored status code
(``PENDING_APPROVAL``) or a field name. Codes stay available to a client in the
error payload (``error.detail``), which is where a screen that branches on them
reads them.

The helpers here are the one place those words come from, so two refusals about
the same month or the same status never word it differently.
"""
from __future__ import annotations

import datetime
import re


def period_label(period, tenant) -> str:
    """How a bursar names ``period``: "August 2026" for a calendar month.

    A period that is not a calendar month (a quarter, or a month that starts mid-way
    through one) keeps a quarter's own name ("Q1 FY2026"), or reads as its dates.
    """
    from vs_config.display import format_date, format_month

    start, end = period.start_date, period.end_date
    if start.day == 1 and (end + datetime.timedelta(days=1)).day == 1 \
            and (start.year, start.month) == (end.year, end.month):
        return format_month(start, tenant, month="long")
    if re.match(r"^\d{4}-\d{2}", period.name or ""):
        return f"{format_date(start, tenant)} to {format_date(end, tenant)}"
    return period.name


def period_words(period) -> str:
    """``period`` as :func:`period_label` names it, read in its own tenant.

    A period that is not a saved fiscal period (the posting guard is duck-typed)
    is named by its own ``name`` or ``label`` when it has one, and as "this
    period" otherwise, never by its ``__str__``.
    """
    entity = getattr(period, "entity", None)
    if entity is not None and getattr(period, "start_date", None) is not None:
        return period_label(period, entity.tenant)
    return str(getattr(period, "name", "") or getattr(period, "label", "") or "this period")


#: How each period status reads in a sentence ("September 2026 is soft-closed").
_PERIOD_STATUS_WORDS = {
    "OPEN": "open",
    "SOFT_CLOSED": "soft-closed",
    "CLOSED": "closed",
    "LOCKED": "locked",
}


def period_status_word(status) -> str:
    """A period or fiscal-year status as a word in a sentence."""
    return _PERIOD_STATUS_WORDS.get(str(status or ""), "not open")


def state_word(document, field: str = "status") -> str:
    """``document``'s ``field`` as its screen names it, lower case: "pending approval".

    Read through the field's own choice labels (``get_<field>_display``), so a
    refusal says "Invoice IV-0012 is posted", never "is 'POSTED'". A value with no
    label (a field without choices) reads as itself, lower case and spaced.
    """
    raw = getattr(document, field, "")
    display = getattr(document, f"get_{field}_display", None)
    label = display() if callable(display) else raw
    return words_for_code(raw) if label == raw else str(label).lower()


def words_for_code(value) -> str:
    """A stored code with no label of its own as words: ``PENDING_APPROVAL`` reads "pending approval"."""
    return str(value or "").replace("_", " ").strip().lower()


def code_words(choices, value) -> str:
    """``value`` of a choices class (``DocumentStatus``) as its label, lower case."""
    try:
        return str(choices(value).label).lower()
    except ValueError:
        return words_for_code(value)


def branch_words(branch):
    """The branch to name in a sentence, or ``None`` where naming one adds nothing.

    At a school with one branch the dimension recedes: its branch is never named.
    ``branch`` is a :class:`~vs_tenants.models.Branch` or its id.
    """
    from vs_rbac.scoping import only_branch_id
    from vs_tenants.models import Branch

    if branch is None:
        return None
    row = branch if hasattr(branch, "name") else (
        Branch.all_objects.filter(pk=branch).only("name", "tenant_id").first()
    )
    if row is None or only_branch_id(row.tenant_id) is not None:
        return None
    return row.name
