"""A workflow audit sentence names the act and the person, and no field's value.

The approvals screens serve ``WorkflowAuditLog.message`` as written, so whatever
a writer interpolates into it reaches every reader of the approval. A document's
amounts, banking details and reasons are the documents' own fields, behind the
switches of the module that owns them; the workflow engine knows none of them.
The one thing a sentence may interpolate is the label of the person who acted.
Reasons and comments travel in the row's ``context``, which no approval screen
serves whole.

The writers are read from source rather than driven, so a sentence added later,
on a path no fixture reaches, is held to the same rule.
"""
from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

SERVICES = Path(__file__).resolve().parent.parent / "services"

#: The only call a workflow sentence may interpolate: it renders a person's name.
PERSON_LABELS = {"_actor_label"}


def _message_arguments():
    """Every ``message=`` argument given to a workflow audit write, with where it is."""
    for path in sorted(SERVICES.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            writes = (
                isinstance(func, ast.Attribute) and func.attr == "write"
                and isinstance(func.value, ast.Name) and func.value.id == "audit_service"
            )
            if not writes:
                continue
            for keyword in node.keywords:
                if keyword.arg == "message":
                    yield f"{path.name}:{node.lineno}", keyword.value


class WorkflowSentenceTests(SimpleTestCase):

    def test_the_writers_are_found(self):
        self.assertGreaterEqual(len(list(_message_arguments())), 2)

    def test_a_sentence_interpolates_only_a_persons_label(self):
        for where, value in _message_arguments():
            for node in ast.walk(value):
                if not isinstance(node, ast.FormattedValue):
                    continue
                expression = node.value
                allowed = (
                    isinstance(expression, ast.Call)
                    and isinstance(expression.func, ast.Name)
                    and expression.func.id in PERSON_LABELS
                )
                self.assertTrue(allowed, f"{where} interpolates {ast.unparse(expression)}")
