"""An audit event keeps its snapshots however the caller built them.

A snapshot taken from a serializer or a model can hold a date, a Decimal or a
UUID. The event's JSON columns refuse such a value, and because emitting never
raises, the refusal used to drop the whole event: a change was made and nothing
recorded it.
"""
import datetime as dt
import uuid
from decimal import Decimal

from django.test import TestCase

from .models import AuditEvent
from .services import emit_audit_event


class JsonSafeSnapshotTests(TestCase):
    def test_a_snapshot_holding_dates_and_decimals_is_stored(self):
        key = uuid.uuid4()
        event = emit_audit_event(
            module_key="STUDENT", action_type="UPDATE", entity_type="Student",
            entity_id="1",
            before_data={"suspension": {"effective_date": dt.date(2026, 3, 9)}},
            diff_data={"fee": {"from": Decimal("1.50"), "to": Decimal("2.00")}},
            metadata={"when": dt.datetime(2026, 3, 9, 8, 30), "ref": key},
        )
        self.assertIsNotNone(event)
        stored = AuditEvent.objects.get(pk=event.pk)
        self.assertEqual(stored.before_data["suspension"]["effective_date"], "2026-03-09")
        self.assertEqual(stored.diff_data["fee"], {"from": "1.50", "to": "2.00"})
        self.assertEqual(stored.metadata["when"], "2026-03-09T08:30:00")
        self.assertEqual(stored.metadata["ref"], str(key))

    def test_a_snapshot_already_safe_is_stored_as_given(self):
        event = emit_audit_event(
            module_key="STUDENT", action_type="UPDATE", entity_type="Student",
            entity_id="2",
            before_data={"a": 1, "b": [1, 2], "c": None, "d": {"e": "f"}},
        )
        self.assertEqual(
            AuditEvent.objects.get(pk=event.pk).before_data,
            {"a": 1, "b": [1, 2], "c": None, "d": {"e": "f"}},
        )
