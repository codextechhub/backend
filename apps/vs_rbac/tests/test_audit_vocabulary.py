"""Every audit action the backend emits is one the central trail will keep.

``AuditEvent`` validates ``action_type`` against
:class:`~vs_audit.models.AuditActionType` on save, and ``emit_audit_event``
swallows the refusal. A type missing from the vocabulary therefore produces no
error anywhere: the RBAC log keeps its row, the platform activity view shows
nothing, and an auditor asking who switched on Read for bank details at Bright
Star finds an empty trail.

Two guards close that. ``record_rbac_audit`` refuses an unregistered type
outright, so the first test through an RBAC path fails. The source scan below
covers the other emitters, where a literal passed as ``action_type`` to
``emit_audit_event`` or ``record_rbac_audit`` must be a registered value.
"""
import ast
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from vs_audit.models import AuditActionType
from vs_rbac.audit import record_rbac_audit
from vs_rbac.models import RBACAuditLog

_EMITTERS = {"emit_audit_event", "record_rbac_audit"}


def _source_files():
    """Every non-test, non-migration module under the apps directory."""
    root = Path(settings.BASE_DIR)
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if "migrations" in parts or "tests" in parts or path.name.startswith("test"):
            continue
        yield root, path


def _literal_action_types():
    """(value, location) for each string literal handed to an emitter."""
    for root, path in _source_files():
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in _EMITTERS:
                continue
            for keyword in node.keywords:
                value = keyword.value
                if keyword.arg == "action_type" and isinstance(value, ast.Constant):
                    yield value.value, f"{path.relative_to(root)}:{node.lineno}"


class LiteralActionTypesAreRegisteredTests(SimpleTestCase):
    def test_every_literal_action_type_is_in_the_vocabulary(self):
        found = list(_literal_action_types())
        unregistered = [
            f"{value!r} at {where}" for value, where in found
            if value not in AuditActionType.values
        ]
        self.assertEqual(unregistered, [], "\n".join(unregistered))


class RecordRbacAuditRefusesUnknownTypesTests(TestCase):
    def test_an_unregistered_type_raises_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            record_rbac_audit(
                action_type="permission_group.create",
                entity_type="permission_group",
                entity_id="1",
            )
        self.assertFalse(RBACAuditLog.objects.exists())

    def test_a_registered_type_writes_both_the_durable_row_and_the_central_event(self):
        from vs_audit.models import AuditEvent

        record_rbac_audit(
            action_type=AuditActionType.FIELD_REGISTRY_SYNCED,
            entity_type="FieldDefinition",
            entity_id="registry",
        )
        self.assertTrue(RBACAuditLog.objects.filter(action_type="FIELD_REGISTRY_SYNCED").exists())
        self.assertTrue(AuditEvent.objects.filter(action_type="FIELD_REGISTRY_SYNCED").exists())
