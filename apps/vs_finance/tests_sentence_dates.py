"""No sentence from finance, procurement or payments prints a raw date.

Every date a person reads goes through :mod:`vs_config.display`, so Corona Group,
which writes "05/01/2026", never meets "2026-01-05" in a refusal while its
screens say otherwise. Fixing the sentences one by one left gaps: a bank split
refusal named its date through a variable called ``unmatched_line``, so a search
for date-like names passed it by. This test reads the source of the three apps
and fails on any f-string that interpolates a date as it stands: a call to
``.isoformat()``, ``str()`` of a date, or a value whose name says it is a date
(``invoice_date``, ``closed_on``, ``created_at``, ``start``) with no format spec.

Three kinds of text are not sentences and are left alone:

* a model's ``__str__`` or ``__repr__``, which a sentence never uses
  (:mod:`vs_finance.wording`);
* management commands, which write to an operator's terminal;
* machine text: an f-string whose literal parts hold no space (a URL query, a
  file name, a lookup key).

A name bound in the same function to text (an f-string, a literal, or the
result of a ``format_*`` or wording helper) is already words and passes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase

APPS_ROOT = Path(__file__).resolve().parent.parent
SCANNED_APPS = ("vs_finance", "vs_procurement", "vs_payments")

#: A name that holds a date or a moment, read from its last word.
DATE_NAME = re.compile(
    r"(^|_)(date|on|at|until|since|due|cutoff|live)$"
    r"|^(day|start|end|earliest|latest|today|when|future)$"
)

#: Helpers whose result is already words.
WORDING_CALLS = re.compile(r"^(format_\w+|period_label|period_words|day|_branch_name|label)$")


def _scanned_files():
    for app in SCANNED_APPS:
        for path in sorted((APPS_ROOT / app).rglob("*.py")):
            parts = path.relative_to(APPS_ROOT).parts
            name = path.name
            if "migrations" in parts or "management" in parts or "tests" in parts:
                continue
            if name.startswith(("tests", "test_")):
                continue
            yield path


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_text(node) -> bool:
    """Whether ``node`` evaluates to words rather than to a date."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) or node.value is None
    if isinstance(node, ast.IfExp):
        return _is_text(node.body) and _is_text(node.orelse)
    if isinstance(node, ast.BinOp):
        return _is_text(node.left) or _is_text(node.right)
    if isinstance(node, ast.BoolOp):
        return all(_is_text(value) for value in node.values)
    if isinstance(node, ast.Call):
        return bool(WORDING_CALLS.match(_call_name(node)))
    return False


def _text_names(function) -> set[str]:
    """Names ``function`` binds to words."""
    names = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            pairs = []
            for target in node.targets:
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    pairs.extend(zip(target.elts, node.value.elts))
                else:
                    pairs.append((target, node.value))
            for target, value in pairs:
                if isinstance(target, ast.Name) and _is_text(value):
                    names.add(target.id)
    return names


def _last_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_raw_date(node, text_names) -> bool:
    if isinstance(node, ast.Call):
        if _call_name(node) == "isoformat":
            return True
        if _call_name(node) == "str" and node.args:
            return _is_raw_date(node.args[0], text_names)
        return False
    name = _last_name(node)
    if not name or not DATE_NAME.search(name):
        return False
    return not (isinstance(node, ast.Name) and name in text_names)


def _is_machine_text(joined) -> bool:
    literal = "".join(
        part.value for part in joined.values
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )
    return " " not in literal


def raw_dates_in(source: str, label: str) -> list[str]:
    """Every date ``source`` interpolates into a sentence without formatting it."""
    tree = ast.parse(source)
    found = []

    def visit(node, function, text_names):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ("__str__", "__repr__"):
                return
            function, text_names = node, _text_names(node)
        if isinstance(node, ast.JoinedStr) and not _is_machine_text(node):
            for part in node.values:
                if (isinstance(part, ast.FormattedValue) and part.format_spec is None
                        and _is_raw_date(part.value, text_names)):
                    expression = ast.get_source_segment(source, part.value)
                    found.append(f"{label}:{part.lineno} {{{expression}}}")
        for child in ast.iter_child_nodes(node):
            visit(child, function, text_names)

    visit(tree, None, set())
    return found


class SentencesFormatTheirDatesTests(SimpleTestCase):

    def test_no_sentence_interpolates_a_raw_date(self):
        found = []
        for path in _scanned_files():
            found += raw_dates_in(path.read_text(), str(path.relative_to(APPS_ROOT)))
        self.assertEqual(found, [], "Write these dates with vs_config.display.format_date.")

    def test_the_guard_sees_a_date_whatever_it_is_called(self):
        source = (
            "def refuse(line, split_date, tenant):\n"
            "    first_on = line.txn_date\n"
            "    shown = format_date(split_date, tenant)\n"
            "    raise Error(f'A line on {first_on} is unmatched after {shown}.')\n"
            "    raise Error(f'Closed on {line.closed_on.isoformat()}.')\n"
            "    raise Error(f'Due {str(line.due_date)}.')\n"
        )
        self.assertEqual(len(raw_dates_in(source, "x")), 3)

    def test_formatted_dates_names_and_machine_text_pass(self):
        source = (
            "def fine(row, tenant):\n"
            "    at = f' at {row.branch}'\n"
            "    start, end = format_date(row.a, tenant), format_date(row.b, tenant)\n"
            "    url = f'/settlement?from={row.start_date.isoformat()}'\n"
            "    name = f'payslip-{row.pay_date:%Y-%m}.pdf'\n"
            "    return f'From {start} to {end}{at}, {format_date(row.due_date, tenant)}.'\n"
            "\n"
            "class Row:\n"
            "    def __str__(self):\n"
            "        return f'Row {self.txn_date}'\n"
        )
        self.assertEqual(raw_dates_in(source, "x"), [])
