"""The attendance report as a file: one table of the report, in the school's own words.

**The file is the screen.** Every table is cut from the same result the report
endpoint serves (:func:`.report.build_report`), passed through the same
serializer, so a figure in a file cannot differ from the figure on the screen
for the same term, branch and caller, and a branch-bound reader's file holds
only their own branches' classes.

**Field Access.** A column of figures is a field the caller may be denied, and a
file with a blank column would still say a figure exists, so the column is left
out. The three tables are cut like this:

* ``by_class``: Class and Branch (Level at a one-branch school) always; Rate,
  Absences, Lateness, Days not taken and Students only when the caller may read
  the register's counts;
* ``chronic``: Student, Student number and Class always; Rate, Days absent,
  Days excused and Days not taken only with the counts. The list itself is a
  statement about a rate, so a caller who may not read the counts receives the
  headings and no rows, exactly as the screen shows no list;
* ``not_taken``: Date, Class, Class teacher and Branch (Level). The list is the
  register's status, so a caller who may not read it receives the headings and
  no rows.

**Reading order.** Dates and rates are written for a person: the date in the
school's date format, a rate as ``87.5%`` and an empty cell where no register
was taken, never a zero. A cell that would open as a formula in a spreadsheet
(it begins with ``=``, ``+``, ``-`` or ``@``) is written as text.
"""
from __future__ import annotations

from django.utils.text import slugify

from vs_config.display import format_date
from vs_finance.exports import ReportTable
from schools.vs_academics.class_teacher_field import may_read_class_teacher
from vs_rbac.field_enforcement import can_read

from ..constants import FIELD_RESOURCE

TABLES = {
    "by_class": "Attendance by class",
    "chronic": "Students below the attendance threshold",
    "not_taken": "Registers not taken",
}

_COUNTS_KEY = f"{FIELD_RESOURCE}.counts"
_STATUS_KEY = f"{FIELD_RESOURCE}.status"
_FORMULA_LEADERS = ("=", "+", "-", "@")


def _text(value) -> str:
    """A cell of identifying text, written so a spreadsheet never runs it."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_LEADERS) else text


def _rate(value) -> str:
    return "" if value is None else f"{value:g}%"


def _where(row: dict, several: bool) -> str:
    """The branch of a row at a school with several, else its level."""
    if several:
        branch = row.get("branch")
        return _text(branch["name"]) if branch else ""
    return _text(row.get("level_name"))


def scope_words(branch, *, several: bool, narrowed: bool) -> str:
    """Which branches a file covers, for its subtitle ("" at a one-branch school)."""
    if not several:
        return ""
    if branch is not None:
        return branch.name
    return "" if narrowed else "All branches"


def filename_for(table: str, term) -> str:
    """``attendance_by_class_first_term``: the table and the term, without the extension."""
    base = f"attendance_{table}"
    slug = slugify(term["name"]).replace("-", "_") if term else ""
    return f"{base}_{slug}" if slug else base


def build_table(request, tenant, report: dict, table: str, *, branch, narrowed: bool) -> ReportTable:
    """*table* of the serialized *report* as a renderer-neutral table.

    *report* is what :class:`..serializers.ReportSerializer` produced for this
    caller, so a block Field Access removed is absent from it.
    """
    several = bool(report["branch_dimension"])
    where = "Branch" if several else "Level"
    counts_open = can_read(request, _COUNTS_KEY)
    status_open = can_read(request, _STATUS_KEY)

    if table == "by_class":
        columns = ["Class", where]
        if counts_open:
            columns += ["Rate", "Absences", "Lateness", "Days not taken", "Students"]
        rows = []
        for item in report.get("by_class", []):
            row = [_text(item["class_name"]), _where(item, several)]
            if counts_open:
                counts = item["counts"]
                row += [
                    _rate(counts["rate"]), counts["absent"], counts["late"],
                    counts["not_taken"], counts["roster_size"],
                ]
            rows.append(row)
    elif table == "chronic":
        columns = ["Student", "Student number", "Class"]
        if counts_open:
            columns += ["Rate", "Days absent", "Days excused", "Days not taken"]
        rows = []
        for item in report.get("chronic", []):
            row = [
                _text(item["student"]["name"]), _text(item["student"]["student_number"]),
                _text(item["class_name"]),
            ]
            if counts_open:
                row += [_rate(item["rate"]), item["absent"], item["excused"], item["not_taken"]]
            rows.append(row)
    else:
        teacher_open = may_read_class_teacher(request)
        columns = ["Date", "Class", *(["Class teacher"] if teacher_open else []), where]
        rows = []
        if status_open:
            for item in report.get("not_taken", []):
                rows.append([
                    format_date(item["date"], tenant), _text(item["class_name"]),
                    *([_text(item["class_teacher_name"])] if teacher_open else []),
                    _where(item, several),
                ])

    term = report.get("term")
    parts = []
    if term:
        parts.append(f"{term['name']}, {term['session_name']}")
    if table == "chronic":
        parts.append(f"below {report['threshold']}%")
    words = scope_words(branch, several=several, narrowed=narrowed)
    if words:
        parts.append(words)
    if narrowed:
        parts.append("the reader's branches only")
    return ReportTable(
        title=TABLES[table], subtitle=" · ".join(parts), columns=columns, rows=rows,
    )
