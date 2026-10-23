"""Accounts a school commonly adds to its chart, offered beside the generic ones.

The finance engine ships a catalogue of accounts any organisation adds. A
school's trade has accounts of its own: the fees it charges and the costs of
teaching. They are registered here, with the FAL, so the engine never names a
school concept. The provider answers only for books owned by a school tenant;
any other tenant's catalogue is the generic one alone.
"""
from __future__ import annotations

from vs_finance.account_guide import AccountSuggestion
from vs_finance.constants import AccountType, IFRSLine
from vs_tenants.models import Tenant

_INCOME = AccountType.INCOME.value
_EXPENSE = AccountType.EXPENSE.value

#: (key, name, account type, description, statutory report line).
_SCHOOL_SUGGESTIONS = tuple(
    AccountSuggestion(key, name, kind, description, line.value)
    for key, name, kind, description, line in (
        ("tuition-fees", "Tuition fees", _INCOME,
         "Fees charged for teaching, per term or session.", IFRSLine.REVENUE),
        ("admission-registration-fees", "Admission and registration fees", _INCOME,
         "One-off fees charged when a pupil is admitted or registered.", IFRSLine.REVENUE),
        ("transport-fees", "Transport fees", _INCOME,
         "Fees charged for the school bus service.", IFRSLine.REVENUE),
        ("boarding-fees", "Boarding fees", _INCOME,
         "Fees charged for lodging and meals of boarders.", IFRSLine.REVENUE),
        ("uniform-book-sales", "Uniform and book sales", _INCOME,
         "Money from selling uniforms, books and other supplies to families.",
         IFRSLine.REVENUE),
        ("examination-fees-received", "Examination fees received", _INCOME,
         "Fees collected from families to cover external examinations.", IFRSLine.REVENUE),
        ("activity-fees", "Extra-curricular activity fees", _INCOME,
         "Fees charged for clubs, lessons and trips outside the timetable.",
         IFRSLine.REVENUE),
        ("teaching-learning-materials", "Teaching and learning materials", _EXPENSE,
         "Textbooks, chalk, charts and other materials used in lessons.",
         IFRSLine.COST_OF_SALES),
        ("examination-board-fees", "Examination board fees paid", _EXPENSE,
         "Fees paid to examination bodies to enter pupils.", IFRSLine.COST_OF_SALES),
        ("sports-school-events", "Sports and school events", _EXPENSE,
         "Equipment, prizes and running costs for sports days and school events.",
         IFRSLine.ADMIN_EXPENSES),
        ("laboratory-supplies", "Laboratory supplies", _EXPENSE,
         "Chemicals, specimens and equipment used in science lessons.",
         IFRSLine.COST_OF_SALES),
    )
)


def school_account_suggestions(entity) -> list[AccountSuggestion]:
    """The school catalogue for a school's books, and nothing for any other tenant's."""
    if getattr(getattr(entity, "tenant", None), "kind", None) != Tenant.Kind.SCHOOL:
        return []
    return list(_SCHOOL_SUGGESTIONS)
