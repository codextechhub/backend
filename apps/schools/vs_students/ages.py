"""How old a student may be, checked the same way wherever a birth date arrives.

A birth date outside the school's age range is a mistyped year, not a pupil:
1998 for 2008 puts a 28-year-old on a school roll and nothing downstream would
question it. The enrolment form, the edit drawer, the spreadsheet import and
the enrol and edit endpoints all ask this one function, so a form, a file and a
direct request refuse the same child for the same reason. Age is counted in
calendar years, as the form counts it.

The range is the school's own (``students.age.min_years`` and
``students.age.max_years``, read through ``services/rules.py``). A school that
has set nothing gets 2 to 25, which is the range every school had before it
could choose; a nursery sets a lower floor and an adult-education centre a
higher ceiling.
"""
from __future__ import annotations

import datetime as dt

from .services.rules import DEFAULT_MAX_AGE, DEFAULT_MIN_AGE

#: The range a school has until it sets its own. Kept under these names because
#: callers outside the module read them.
MIN_AGE_YEARS = DEFAULT_MIN_AGE
MAX_AGE_YEARS = DEFAULT_MAX_AGE


def date_of_birth_problem(
    dob: dt.date, *, tenant=None, today: dt.date | None = None, bounds=None,
) -> str:
    """The sentence to show for an implausible birth date, or "" when it is fine.

    *bounds* is ``(youngest, oldest)`` already read for *tenant*, which a
    caller checking many rows passes so the range is read once rather than per
    row. Without either, the default range applies.
    """
    if bounds is None:
        if tenant is not None:
            from .services.rules import age_bounds

            bounds = age_bounds(tenant)
        else:
            bounds = (MIN_AGE_YEARS, MAX_AGE_YEARS)
    youngest, oldest = bounds
    # TODO: take the day from vs_config.clock.tenant_today(tenant).
    today = today or dt.date.today()
    if dob > today:
        return "That date is in the future."
    years = today.year - dob.year
    if years < youngest:
        return (
            f"That would make the student under {youngest} years old, and "
            f"this school enrols students from {youngest}. Check the year."
        )
    if years > oldest:
        return (
            f"That would make the student over {oldest}, and this school "
            f"enrols students up to {oldest}. Check the year."
        )
    return ""
