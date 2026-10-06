"""Nothing a person reads mentions kobo.

Money is stored and computed as whole kobo, and every amount a person reads is
in naira, written the way ``vs_finance.money.format_naira`` writes it:
"₦4,929,000.00". Kobo reaches a screen in two ways. A sentence drops the raw
integer into an f-string and names the unit after it::

    detail=f"GR/IR clearing balance {balance} kobo"

which a bursar closing September reads as "GR/IR clearing balance 492900000
kobo". Or a validation message tells the reader to send an amount "in kobo",
a unit nobody at a school counts in. Exception messages, validation errors,
audit-log messages, close-check details, notification context, workflow
summaries and model ``__str__`` text all reach a screen, so the rule holds for
every one of them.

The scan reads every module under ``apps/`` except tests, migrations,
management commands (their output goes to an operator's terminal) and
``__pycache__``, and refuses any string literal or f-string whose text contains
the word "kobo" standing on its own. These are allowed:

- documentation: a docstring, or any bare string statement such as the one
  documenting a module-level name, since it never runs;
- a name rather than prose: a literal that is the bare word ``"kobo"`` in any
  case, as in the ``{"kobo": ..., "naira": ...}`` money envelope, an
  ``__all__`` entry or an enum value. An identifier such as ``amount_kobo``
  never matches at all, because the underscore makes it part of a longer word;
- a ``help_text=`` keyword, which documents a stored column for the admin and
  the API schema, where saying the column holds kobo is the accurate contract;
- the arguments of a logger call (``logger.info(...)``, ``log.warning(...)``),
  which go to the server log rather than to a person;
- ``PERMANENT_EXCEPTIONS``, located by path under ``apps/`` and the function or
  class the string sits in: ``naira_in_words`` spells an amount out for a
  receipt ("One thousand naira, fifty kobo"), and ``MoneyField`` describes its
  own storage unit.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase

APPS_DIR = Path(__file__).resolve().parent.parent

SKIPPED_DIR_NAMES = frozenset({"migrations", "tests", "management", "__pycache__"})

#: Where "kobo" is the right word, by path and enclosing function or class.
PERMANENT_EXCEPTIONS = frozenset({
    ("vs_finance/money.py", "naira_in_words"),
    ("vs_finance/money.py", "MoneyField"),
})

#: Logger methods whose arguments go to the server log.
LOGGER_METHODS = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical", "log",
})

# The unit as a word of its own: "in kobo", "(kobo)", but not "amount_kobo".
_KOBO_WORD = re.compile(r"(?<![A-Za-z0-9_])kobo(?![A-Za-z0-9_])", re.IGNORECASE)


def _is_source_module(path: Path) -> bool:
    """Whether ``path`` holds runtime code, rather than a test, migration or command."""
    if SKIPPED_DIR_NAMES.intersection(path.relative_to(APPS_DIR).parts):
        return False
    return not path.name.startswith("test")


def _documentation_ids(tree: ast.AST) -> set[int]:
    """The ids of the string constants that stand alone as statements in ``tree``.

    That covers every docstring and the string that documents a module-level
    name; neither is evaluated for its value.
    """
    return {
        id(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _is_logger_call(node: ast.Call) -> bool:
    """Whether ``node`` calls a logging method on something named like a logger."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in LOGGER_METHODS):
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name):
        name = receiver.id
    elif isinstance(receiver, ast.Attribute):
        name = receiver.attr
    else:
        return False
    return "log" in name.lower()


def _names_the_unit(text: str) -> bool:
    """Whether ``text`` uses "kobo" as a word, other than as a bare key or name."""
    if text.strip().lower() == "kobo":
        return False
    return bool(_KOBO_WORD.search(text))


class _KoboFinder(ast.NodeVisitor):
    """Collect ``(line, enclosing names, text)`` for each string that mentions kobo."""

    def __init__(self, documentation: set[int]):
        self.documentation = documentation
        self.scope: list[str] = []
        self.hits: list[tuple[int, tuple[str, ...], str]] = []

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_scope

    def visit_Call(self, node: ast.Call):
        if not _is_logger_call(node):
            self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword):
        if node.arg != "help_text":
            self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr):
        text = "".join(
            part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
        if _names_the_unit(text):
            self.hits.append((node.lineno, tuple(self.scope), text.strip()))
        # The literal parts are checked above as one text; only the values remain.
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                self.visit(part.value)

    def visit_Constant(self, node: ast.Constant):
        if (
            isinstance(node.value, str)
            and id(node) not in self.documentation
            and _names_the_unit(node.value)
        ):
            self.hits.append((node.lineno, tuple(self.scope), node.value.strip()))


def kobo_wording_in(source: str) -> list[tuple[int, tuple[str, ...], str]]:
    """Every string in ``source`` that mentions kobo where a person could read it."""
    tree = ast.parse(source)
    finder = _KoboFinder(_documentation_ids(tree))
    finder.visit(tree)
    return finder.hits


def _excused(relative: str, scope: tuple[str, ...]) -> bool:
    return any(
        path == relative and name in scope for path, name in PERMANENT_EXCEPTIONS
    )


class NoAmountIsShownInKoboTests(SimpleTestCase):
    def test_no_runtime_string_mentions_kobo(self):
        offenders = []
        for path in sorted(APPS_DIR.rglob("*.py")):
            if not _is_source_module(path):
                continue
            relative = path.relative_to(APPS_DIR).as_posix()
            for line, scope, text in kobo_wording_in(path.read_text(encoding="utf-8")):
                if not _excused(relative, scope):
                    offenders.append(f"apps/{relative}:{line}: {text[:80]}")
        self.assertEqual(
            offenders, [],
            "Show amounts in naira with vs_finance.money.format_naira, and ask for "
            "amounts without naming kobo:\n" + "\n".join(offenders),
        )

    def test_the_scan_recognises_each_shape_of_kobo_wording(self):
        source = (
            'def check(balance, debit):\n'
            '    """Report {balance} kobo, which a docstring may say."""\n'
            '    a = f"GR/IR clearing balance {balance} kobo (received not invoiced)"\n'
            '    b = f"debits={debit} (kobo)"\n'
            '    c = "%s kobo" % balance\n'
            '    d = "{} kobo".format(balance)\n'
            '    e = "Amount: {{ amount }} kobo"\n'
            '    f = f"GR/IR clearing balance {balance}"\n'
            '    g = "Expected a whole integer amount in kobo."\n'
            '    h = f"{debit}: an amount is whole Kobo."\n'
        )
        self.assertEqual(
            [line for line, _, _ in kobo_wording_in(source)], [3, 4, 5, 6, 7, 9, 10],
        )

    def test_keys_documentation_columns_and_logs_are_left_alone(self):
        source = (
            'Kobo = int\n'
            '"""An amount in integer minor units (kobo)."""\n'
            '__all__ = ["Kobo"]\n'
            'def envelope(value, row):\n'
            '    logger.info("Swept %s kobo", value)\n'
            '    self.log.warning(f"{value} kobo left")\n'
            '    total = MoneyField(help_text="Total, in kobo.")\n'
            '    return {"kobo": value, "amount_kobo": value, "KOBO": row["kobo"]}\n'
        )
        self.assertEqual(kobo_wording_in(source), [])

    def test_an_excuse_covers_only_its_own_function(self):
        self.assertTrue(_excused("vs_finance/money.py", ("naira_in_words",)))
        self.assertTrue(_excused("vs_finance/money.py", ("MoneyField", "__init__")))
        self.assertFalse(_excused("vs_finance/money.py", ("format_naira",)))
