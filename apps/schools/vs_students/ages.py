"""How old a student may be, checked the same way wherever a birth date arrives.

Under 2 or over 25 is a mistyped year, not a pupil: 1998 for 2008 puts a
28-year-old on a school roll and nothing downstream would question it. The
enrolment form, the edit drawer, the spreadsheet import and the enrol and edit
endpoints all ask this one function, so a form, a file and a direct request
refuse the same child for the same reason. Age is counted in calendar years,
as the form counts it.
"""
from __future__ import annotations

import datetime as dt

MIN_AGE_YEARS = 2
MAX_AGE_YEARS = 25


def date_of_birth_problem(dob: dt.date, *, today: dt.date | None = None) -> str:
    """The sentence to show for an implausible birth date, or "" when it is fine."""
    today = today or dt.date.today()
    if dob > today:
        return "That date is in the future."
    years = today.year - dob.year
    if years < MIN_AGE_YEARS:
        return (
            f"That would make the student under {MIN_AGE_YEARS} years old. "
            f"Check the year."
        )
    if years > MAX_AGE_YEARS:
        return f"That would make the student over {MAX_AGE_YEARS}. Check the year."
    return ""
