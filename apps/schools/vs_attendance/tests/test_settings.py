"""A school's attendance settings: declared once, read cleaned, and never drifting.

Four definitions are declared by migration (so every database has them) and by
``seed_config_catalogue`` (so a rebuilt catalogue does); the two must agree.
"""
from __future__ import annotations

import importlib

from django.test import SimpleTestCase, TestCase

from vs_config.exceptions import InvalidConfigurationValue
from vs_config.management.commands.seed_config_catalogue import SCHOOL_SCOPED_DEFINITIONS
from vs_config.models import ConfigurationDefinition

from ..constants import (
    CFG_CHRONIC_THRESHOLD,
    CFG_EDIT_WINDOW_DAYS,
    CFG_GUARDIAN_NOTICES,
    CFG_LESSON_MARKING_LEVELS,
)
from ..services import rules
from ..services.rules import AttendanceRules, read_attendance_rules
from .base import AttendanceFixture, set_setting

migration_0002 = importlib.import_module(
    "schools.vs_attendance.migrations.0002_attendance_settings"
)
KEYS = (
    CFG_GUARDIAN_NOTICES, CFG_CHRONIC_THRESHOLD,
    CFG_LESSON_MARKING_LEVELS, CFG_EDIT_WINDOW_DAYS,
)


class DeclarationTests(TestCase):
    def test_every_database_has_the_four_definitions_at_school_scope(self):
        rows = {d.key: d for d in ConfigurationDefinition.objects.filter(key__in=KEYS)}
        self.assertEqual(set(rows), set(KEYS))
        for row in rows.values():
            self.assertEqual(sorted(row.allowed_scopes), ["platform", "school"])

    def test_the_defaults_are_what_a_school_gets_before_it_chooses(self):
        defaults = {
            d.key: d.default_value
            for d in ConfigurationDefinition.objects.filter(key__in=KEYS)
        }
        self.assertEqual(defaults, {
            CFG_GUARDIAN_NOTICES: True,
            CFG_CHRONIC_THRESHOLD: 90,
            CFG_LESSON_MARKING_LEVELS: [],
            CFG_EDIT_WINDOW_DAYS: 0,
        })

    def test_the_catalogue_seed_declares_exactly_what_the_migration_does(self):
        seeded = {
            row[0]: row for row in SCHOOL_SCOPED_DEFINITIONS if row[0] in KEYS
        }
        self.assertEqual(set(seeded), set(KEYS))
        for definition in migration_0002.DEFINITIONS:
            key, label, description, value_type, default, validation = seeded[definition["key"]]
            with self.subTest(key=key):
                self.assertEqual(label, definition["label"])
                self.assertEqual(description, definition["description"])
                self.assertEqual(value_type, definition["value_type"])
                self.assertEqual(default, definition["default_value"])
                self.assertEqual(validation, definition["validation_rules"])


class ReadTests(AttendanceFixture):
    def test_a_school_that_has_chosen_nothing_reads_the_defaults(self):
        self.assertEqual(read_attendance_rules(self.tenant), AttendanceRules())
        self.assertEqual(
            AttendanceRules(),
            AttendanceRules(
                guardian_notices=True, chronic_threshold=90,
                lesson_marking_levels=(), edit_window_days=0,
            ),
        )

    def test_a_school_reads_its_own_values_and_no_other_schools(self):
        set_setting(CFG_EDIT_WINDOW_DAYS, 3, self.tenant, self.admin)
        set_setting(CFG_CHRONIC_THRESHOLD, 85, self.tenant, self.admin)
        set_setting(CFG_GUARDIAN_NOTICES, False, self.tenant, self.admin)
        set_setting(CFG_LESSON_MARKING_LEVELS, ["JSS1", "JSS2"], self.tenant, self.admin)

        mine = read_attendance_rules(self.tenant)
        self.assertEqual(
            (mine.edit_window_days, mine.chronic_threshold, mine.guardian_notices,
             mine.lesson_marking_levels),
            (3, 85, False, ("JSS1", "JSS2")),
        )
        self.assertEqual(read_attendance_rules(self.solo), AttendanceRules())

    def test_the_bounds_are_enforced_when_a_school_saves_one(self):
        for key, bad in (
            (CFG_CHRONIC_THRESHOLD, 49), (CFG_CHRONIC_THRESHOLD, 101),
            (CFG_EDIT_WINDOW_DAYS, -1), (CFG_EDIT_WINDOW_DAYS, 15),
            (CFG_GUARDIAN_NOTICES, "yes"),
        ):
            with self.subTest(key=key, value=bad):
                with self.assertRaises(InvalidConfigurationValue):
                    set_setting(key, bad, self.tenant, self.admin)

    def test_a_reader_asks_only_for_the_settings_it_needs(self):
        self.assertEqual(rules.read_edit_window_days(self.tenant), 0)
        self.assertTrue(rules.read_guardian_notices(self.tenant))
        self.assertEqual(read_attendance_rules(None), AttendanceRules())


class CleaningTests(SimpleTestCase):
    """A value stored by hand at the platform layer reads as the default."""

    def test_a_threshold_outside_fifty_to_one_hundred_reads_as_ninety(self):
        for bad in (49, 101, "90", True, None, 90.5):
            with self.subTest(bad=bad):
                self.assertEqual(rules._clean_threshold(bad), 90)
        self.assertEqual(rules._clean_threshold(50), 50)
        self.assertEqual(rules._clean_threshold(100), 100)

    def test_a_window_outside_nought_to_fourteen_reads_as_the_same_day(self):
        for bad in (-1, 15, "1", True, None):
            with self.subTest(bad=bad):
                self.assertEqual(rules._clean_window(bad), 0)
        self.assertEqual(rules._clean_window(14), 14)

    def test_lesson_marking_levels_are_a_list_of_codes_or_nothing(self):
        self.assertEqual(rules._clean_levels(["JSS1", " JSS2 ", "JSS1"]), ("JSS1", "JSS2"))
        for bad in ("JSS1", None, ["JSS1", ""], ["JSS1", 2]):
            with self.subTest(bad=bad):
                self.assertEqual(rules._clean_levels(bad), ())
