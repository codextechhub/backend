"""
Tests for the durable RBAC audit log (B21 hybrid pattern).
"""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from vs_rbac.audit import record_rbac_audit
from vs_rbac.models import RBACAuditLog

from .helpers import make_school, make_branch, make_school_admin, make_role, make_assignment


class RecordRBACAuditTests(TestCase):
    def test_writes_durable_row_and_mirrors(self):
        log = record_rbac_audit(
            action_type="ROLE_ASSIGNED",
            entity_type="User",
            entity_id="42",
            summary="Test event",
            metadata={"school_id": "test-school"},
        )
        self.assertIsNotNone(log.pk)
        self.assertEqual(log.school_id, "test-school")
        self.assertEqual(RBACAuditLog.objects.count(), 1)

    def test_mirror_failure_does_not_break_the_action(self):
        # The central mirror is best-effort; even if it blows up internally the
        # durable row must survive and no exception may escape.
        with patch("vs_audit.services.emit_audit_event", side_effect=RuntimeError("boom")):
            try:
                record_rbac_audit(
                    action_type="ROLE_CHANGED",
                    entity_type="User",
                    entity_id="1",
                )
            except RuntimeError:
                self.fail("Mirror failure must not propagate.")
        self.assertEqual(RBACAuditLog.objects.count(), 1)

    def test_rows_are_immutable(self):
        log = record_rbac_audit(
            action_type="ROLE_ASSIGNED", entity_type="User", entity_id="7",
        )
        log.summary = "tampered"
        with self.assertRaises(ValidationError):
            log.save()
        with self.assertRaises(ValidationError):
            log.delete()

    def test_role_assignment_writes_durable_audit(self):
        school = make_school()
        branch = make_branch(school)
        admin = make_school_admin(branch)
        role = make_role(school)

        before = RBACAuditLog.objects.count()
        make_assignment(school, admin, role)
        after = RBACAuditLog.objects.filter(action_type__icontains="ROLE").count()
        self.assertGreater(RBACAuditLog.objects.count(), before)
        self.assertGreater(after, 0)
