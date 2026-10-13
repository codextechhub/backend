"""Protected words and values in an audit row, decided without a database.

A rule is declared as data, so the same answer can be asked of one row in
Python and of the whole table in SQL. These tests hold the Python half: which
rows a rule names, which keys it removes from the metadata and the snapshots
(a dotted path reaches inside a nested dictionary), and that a rule guarding
values never cuts a summary.
"""
from types import SimpleNamespace

from django.db.models import F, Value
from django.test import SimpleTestCase

from . import protected_words
from .protected_words import (
    ProtectedWords,
    rule_for,
    visible_metadata,
    visible_snapshot,
    visible_summary,
    visible_summary_expression,
)


def event(entity_type="Ledger", action_type="CONFIG_CHANGED", summary="", **metadata):
    return SimpleNamespace(
        entity_type=entity_type, action_type=action_type, summary=summary,
        metadata=metadata,
    )


class _Rules(SimpleTestCase):
    def setUp(self):
        """Run against a registry this test controls, whatever the apps registered."""
        for registry in (protected_words._RULES, protected_words._RECORD_ONLY):
            saved = dict(registry)
            registry.clear()
            self.addCleanup(lambda r=registry, s=saved: (r.clear(), r.update(s)))

    def register(self, name, **kwargs):
        rule = ProtectedWords(
            entity_type="Ledger", action_types=frozenset({"CONFIG_CHANGED"}), **kwargs,
        )
        protected_words.register_protected_words(name, rule)
        return rule


class MetadataValueTests(_Rules):
    def test_a_dotted_path_removes_a_nested_value_from_a_copy(self):
        self.register(
            "ends", field_key="x.y.ends", metadata_key="",
            metadata_hide=frozenset({"before.ends_at", "after.ends_at"}),
        )
        row = event(
            before={"ends_at": "2027-01-01", "state": "GRANTED"},
            after={"ends_at": "2028-01-01", "state": "GRANTED"},
        )
        shown = visible_metadata(row, lambda key: False)
        self.assertEqual(shown, {
            "before": {"state": "GRANTED"}, "after": {"state": "GRANTED"},
        })
        self.assertEqual(row.metadata["before"]["ends_at"], "2027-01-01")

    def test_a_reader_of_the_field_gets_the_row_as_written(self):
        self.register(
            "ends", field_key="x.y.ends", metadata_key="",
            metadata_hide=frozenset({"before.ends_at"}),
        )
        row = event(before={"ends_at": "2027-01-01"}, after={})
        self.assertIs(visible_metadata(row, lambda key: True), row.metadata)

    def test_a_path_that_leads_nowhere_removes_nothing(self):
        self.register(
            "ends", field_key="x.y.ends", metadata_key="",
            metadata_hide=frozenset({"before.ends_at", "missing.deep.key"}),
        )
        row = event(before="not a dictionary", after={})
        self.assertEqual(visible_metadata(row, lambda key: False), row.metadata)

    def test_a_rule_without_words_leaves_the_summary_and_the_search_alone(self):
        rule = self.register(
            "ends", field_key="x.y.ends", metadata_key="",
            metadata_hide=frozenset({"before.ends_at"}),
        )
        row = event(summary="Changed. Reason: because", before={}, after={})
        self.assertFalse(rule.carries_words)
        self.assertIsNone(rule_for(row))
        self.assertEqual(visible_summary(row, lambda key: False), row.summary)
        self.assertEqual(str(visible_summary_expression(lambda key: False)), "F(summary)")

    def test_two_rules_on_one_row_each_cut_their_own_keys(self):
        self.register(
            "a", field_key="x.y.a", metadata_key="", metadata_hide=frozenset({"a"}),
        )
        self.register(
            "b", field_key="x.y.b", metadata_key="", metadata_hide=frozenset({"b"}),
        )
        row = event(a=1, b=2, c=3)
        self.assertEqual(
            visible_metadata(row, lambda key: key == "x.y.a"), {"a": 1, "c": 3},
        )
        self.assertEqual(visible_metadata(row, lambda key: False), {"c": 3})

    def test_words_and_hidden_keys_go_together(self):
        self.register(
            "move", field_key="x.y.status", metadata_key="reason",
            summary_marker=" from ", metadata_hide=frozenset({"to_status"}),
        )
        row = event(
            summary="Moved from A to B.", reason="Misconduct", to_status="B", kept=1,
        )
        self.assertEqual(visible_summary(row, lambda key: False), "Moved")
        self.assertEqual(visible_metadata(row, lambda key: False), {"kept": 1})


class MatchTests(_Rules):
    def test_metadata_match_narrows_a_rule_to_one_kind_of_change(self):
        rule = self.register(
            "cleared", field_key="x.y.z", metadata_key="",
            metadata_hide=frozenset({"before"}),
            metadata_match=(("config_action", "config.depth_grant.cleared"),),
        )
        cleared = event(config_action="config.depth_grant.cleared", before={"depth": 2})
        other = event(config_action="config.capability.updated", before={"depth": 2})
        self.assertTrue(rule.matches(cleared))
        self.assertFalse(rule.matches(other))
        self.assertEqual(visible_metadata(other, lambda key: False), other.metadata)
        self.assertEqual(visible_metadata(cleared, lambda key: False), {
            "config_action": "config.depth_grant.cleared",
        })

    def test_the_sql_form_carries_the_same_conditions(self):
        rule = ProtectedWords(
            field_key="x.y.z", entity_type="Ledger",
            action_types=frozenset({"CONFIG_CHANGED"}),
            metadata_keys=frozenset({"before"}),
            metadata_match=(("config_action", "c"),),
        )
        text = str(rule.q())
        self.assertIn("metadata__has_keys", text)
        self.assertIn("metadata__contains", text)


class SnapshotPathTests(_Rules):
    def test_a_snapshot_rule_reaches_a_nested_key(self):
        self.register(
            "reason", field_key="x.y.reason", snapshot_keys=frozenset({"suspension.reason"}),
        )
        snapshot = {"suspension": {"reason": "Fighting", "due_back": False}, "age": 12}
        shown = visible_snapshot(event(), snapshot, lambda key: False)
        self.assertEqual(shown, {"suspension": {"due_back": False}, "age": 12})
        self.assertEqual(snapshot["suspension"]["reason"], "Fighting")

    def test_a_rule_naming_the_name_parts_hides_the_one_line_name_with_any_of_them(self):
        for part in ("first_name", "last_name"):
            self.register(
                part, field_key=f"x.y.{part}",
                snapshot_keys=frozenset({part, "full_name"}),
            )
        snapshot = {"first_name": "Tunde", "last_name": "Bello", "full_name": "Tunde Bello"}
        self.assertEqual(
            visible_snapshot(event(), snapshot, lambda key: key == "x.y.first_name"),
            {"first_name": "Tunde"},
        )


class RecordOnlyTests(_Rules):
    """A module's rows that name an act and never a value."""

    def register_rule(self):
        rule = protected_words.RecordOnly(
            module_key="LEDGER", entity_prefix="ledger.", action_types=frozenset({"POSTED"}),
            required_keys=frozenset({"act"}), metadata_allow=frozenset({"act", "marker"}),
            marker="marker", summary_for=lambda row: f"Rebuilt {row.entity_label}",
            summary_sql=lambda: Value("Rebuilt"),
        )
        protected_words.register_record_only("ledger", rule)
        return rule

    def row(self, **metadata):
        return SimpleNamespace(
            module_key="LEDGER", entity_type="ledger.Invoice", action_type="POSTED",
            entity_label="INV-1", summary="Posted for EZE (5,000)", metadata=metadata,
        )

    def test_an_older_row_is_rebuilt_and_cut_to_the_allow_list(self):
        self.register_rule()
        row = self.row(act="X", total=500_000)
        self.assertEqual(visible_summary(row, lambda key: True), "Rebuilt INV-1")
        self.assertEqual(visible_metadata(row, lambda key: True), {"act": "X"})
        self.assertEqual(visible_snapshot(row, {"total": 1}, lambda key: True), {})

    def test_a_row_written_without_values_keeps_its_stored_sentence(self):
        self.register_rule()
        row = self.row(act="X", marker=True)
        row.summary = "Invoice posted: INV-1"
        self.assertEqual(visible_summary(row, lambda key: False), "Invoice posted: INV-1")

    def test_a_row_of_another_module_or_action_is_untouched(self):
        self.register_rule()
        other = self.row(act="X", total=1)
        other.module_key = "CONFIG"
        self.assertEqual(visible_summary(other, lambda key: False), other.summary)
        self.assertEqual(visible_metadata(other, lambda key: False), other.metadata)
        unmarked = self.row(total=1)
        self.assertEqual(visible_summary(unmarked, lambda key: False), unmarked.summary)

    def test_the_sql_form_replaces_only_older_rows(self):
        rule = self.register_rule()
        text = str(rule.legacy_q())
        self.assertIn("module_key", text)
        self.assertIn("marker", text)
        self.assertNotIsInstance(visible_summary_expression(lambda key: True), F)
