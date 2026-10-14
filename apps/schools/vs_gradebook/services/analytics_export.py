"""The analytics tables as files: one table of the screen, in the school's own words.

**The file is the screen.** A table is cut from the payload
:class:`~..serializers.AnalyticsSerializer` produced for this caller from
:func:`.analytics.report`, the same one the screen is drawn from, so a figure in
a file cannot differ from the figure on the screen for the same term, branch and
caller, and a branch-bound reader's file holds only their own branches' classes.

**Field Access.** A column of figures is a field the caller may be denied, and a
file with a blank column would still say a figure exists, so the column is left
out:

* ``subjects``: Subject, Class and Marked always (and Branch where the school has
  several); Average, Pass rate and Grade spread only when the caller may read the
  class figures of a sheet;
* ``movers``: Student, Admission number, Class and Compared with always (and
  Branch where the school has several); Previous average, Average this term and
  Change only when the caller may read the overall average. A caller who may not
  receives the names alone, in name order, as the screen lists them.

A pass rate is written ``85%``, an average as the two-place figure the screen
shows, and the spread as the grades that were earned with their counts
(``A 3, B 5, F 1``). A cell that would open as a formula in a spreadsheet (it
begins with ``=``, ``+``, ``-`` or ``@``) is written as text, except the signed
change, which is a number.
"""
from __future__ import annotations

from django.utils.text import slugify

from schools.vs_attendance.services.export import scope_words
from vs_finance.exports import ReportTable
from vs_rbac.field_enforcement import can_read

from ..constants import OVERALL_AVERAGE_FIELD

CLASS_STATS_FIELD = "gradebook.sheet.class_stats"

TABLES = {
    "subjects": "Performance by subject",
    "movers": "Change in overall average",
}

_FORMULA_LEADERS = ("=", "+", "-", "@")


def _text(value) -> str:
    """A cell of identifying text, written so a spreadsheet never runs it."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_LEADERS) else text


def _percent(value) -> str:
    return "" if value is None else f"{value}%"


def _spread(entries) -> str:
    """The grades that were earned with their counts, in scale order."""
    return ", ".join(f"{entry['grade']} {entry['count']}" for entry in entries if entry["count"])


def filename_for(table: str, term) -> str:
    """``gradebook_subjects_first_term``: the table and the term, without the extension."""
    slug = slugify(term["name"]).replace("-", "_") if term else ""
    base = f"gradebook_{table}"
    return f"{base}_{slug}" if slug else base


def build_table(request, report: dict, table: str, *, branch, narrowed: bool) -> ReportTable:
    """*table* of the serialized analytics *report* as a renderer-neutral table."""
    scope = report["scope"]
    several = bool(scope["branch_dimension"])
    homes = {row["id"]: row["branch"]["name"] for row in scope["classes"] if row["branch"]}

    def where(class_id):
        return [_text(homes.get(class_id, ""))] if several else []

    if table == "subjects":
        stats = can_read(request, CLASS_STATS_FIELD)
        columns = ["Subject", "Class"] + (["Branch"] if several else [])
        columns += ["Average", "Pass rate", "Grade spread"] if stats else []
        columns += ["Marked"]
        rows = []
        for item in report["subjects"]:
            row = [_text(item["subject"]["name"]), _text(item["class"]["name"])]
            row += where(item["class"]["id"])
            if stats:
                row += [item["average"] or "", _percent(item["pass_rate"]), _spread(item["spread"])]
            row.append(item["marked"])
            rows.append(row)
    else:
        average = can_read(request, OVERALL_AVERAGE_FIELD)
        columns = ["Student", "Admission number", "Class"] + (["Branch"] if several else [])
        columns += ["Compared with"]
        columns += ["Previous average", "Average this term", "Change"] if average else []
        rows = []
        for item in report["movers"]:
            row = [
                _text(item["student"]["name"]), _text(item["student"]["admission_number"]),
                _text(item["class"]["name"]),
            ]
            row += where(item["class"]["id"])
            row.append(_text(item["from_term"]["name"]))
            if average:
                row += [item["from"], item["to"], item["change"]]
            rows.append(row)

    term = report["term"]
    parts = [f"{term['name']}, {term['session']}"]
    words = scope_words(branch, several=several, narrowed=narrowed)
    if words:
        parts.append(words)
    if narrowed:
        parts.append("the reader's branches only")
    return ReportTable(
        title=TABLES[table], subtitle=" · ".join(parts), columns=columns, rows=rows,
    )
