"""The gradebook settings: declared, read with defaults, validated, saved and audited."""
from __future__ import annotations

import importlib

from django.test import TestCase

from vs_config.management.commands.seed_config_catalogue import SCHOOL_SCOPED_DEFINITIONS
from vs_config.models import ConfigurationAuditEvent, ConfigurationDefinition

from ..constants import (
    CFG_CONDUCT_RATINGS,
    CFG_MODERATED_PROGRAMS,
    CFG_RANK_IN_CLASS,
    CFG_RATING_KEY,
    CFG_RATING_TRAITS,
    CFG_SHOW_CLASS_STATS,
    CFG_TELL_EVERY_PARENT,
    DEFAULT_RATING_TRAITS,
)
from ..services.rules import GradebookRules, read_gradebook_rules
from .base import GradebookFixture, set_setting

migration_0001 = importlib.import_module("schools.vs_gradebook.migrations.0001_initial")
migration_0004 = importlib.import_module("schools.vs_gradebook.migrations.0004_tell_every_parent")
KEYS = (
    CFG_SHOW_CLASS_STATS, CFG_RANK_IN_CLASS, CFG_CONDUCT_RATINGS, CFG_RATING_KEY,
    CFG_RATING_TRAITS, CFG_MODERATED_PROGRAMS,
)


class DeclarationTests(TestCase):
    def test_every_database_has_the_six_definitions_at_school_scope(self):
        rows = {d.key: d for d in ConfigurationDefinition.objects.filter(key__in=KEYS)}
        self.assertEqual(set(rows), set(KEYS))
        for row in rows.values():
            self.assertEqual(sorted(row.allowed_scopes), ["platform", "school"])

    def test_the_defaults_are_what_a_school_gets_before_it_chooses(self):
        defaults = {
            d.key: d.default_value for d in ConfigurationDefinition.objects.filter(key__in=KEYS)
        }
        self.assertEqual(defaults, {
            CFG_SHOW_CLASS_STATS: True, CFG_RANK_IN_CLASS: True,
            CFG_CONDUCT_RATINGS: True, CFG_RATING_KEY: True,
            CFG_RATING_TRAITS: list(DEFAULT_RATING_TRAITS), CFG_MODERATED_PROGRAMS: [],
        })
        self.assertEqual(len(DEFAULT_RATING_TRAITS), 10)

    def test_the_catalogue_seed_declares_exactly_what_the_migration_does(self):
        seeded = {row[0]: row for row in SCHOOL_SCOPED_DEFINITIONS if row[0] in KEYS}
        self.assertEqual(set(seeded), set(KEYS))
        for definition in migration_0001.DEFINITIONS:
            key, label, description, value_type, default, validation = seeded[definition["key"]]
            with self.subTest(key=key):
                self.assertEqual(label, definition["label"])
                self.assertEqual(description, definition["description"])
                self.assertEqual(value_type, definition["value_type"])
                self.assertEqual(default, definition["default_value"])
                self.assertEqual(validation, definition["validation_rules"])


class ReadTests(GradebookFixture):
    def test_a_school_that_has_chosen_nothing_reads_the_defaults(self):
        self.assertEqual(read_gradebook_rules(self.tenant), GradebookRules())
        rules = read_gradebook_rules(self.tenant)
        self.assertEqual(rules.rating_traits, DEFAULT_RATING_TRAITS)
        self.assertEqual(rules.moderated_programs, ())

    def test_a_school_reads_its_own_values_and_no_other_schools(self):
        set_setting(CFG_RANK_IN_CLASS, False, self.tenant, self.admin)
        set_setting(CFG_RATING_TRAITS, ["Punctuality", "Teamwork"], self.tenant, self.admin)
        set_setting(CFG_MODERATED_PROGRAMS, ["JSS"], self.tenant, self.admin)
        mine = read_gradebook_rules(self.tenant)
        self.assertFalse(mine.rank_in_class)
        self.assertEqual(mine.rating_traits, ("Punctuality", "Teamwork"))
        self.assertTrue(mine.moderates("JSS"))
        self.assertTrue(mine.moderates(self.junior))
        self.assertFalse(mine.moderates(self.senior))
        self.assertEqual(read_gradebook_rules(self.solo), GradebookRules())

    def test_a_bad_stored_value_reads_as_the_default(self):
        for key, bad in (
            (CFG_RATING_TRAITS, []), (CFG_RATING_TRAITS, ["A", "a"]),
            (CFG_RATING_TRAITS, ["x" * 61]), (CFG_RATING_TRAITS, [str(i) for i in range(16)]),
            (CFG_RATING_TRAITS, [" "]), (CFG_RATING_TRAITS, [1, 2]),
        ):
            with self.subTest(value=bad):
                set_setting(key, bad, self.tenant, self.admin)
                self.assertEqual(read_gradebook_rules(self.tenant).rating_traits, DEFAULT_RATING_TRAITS)
        for bad in ([1], ["JSS", ""], [None]):
            with self.subTest(value=bad):
                set_setting(CFG_MODERATED_PROGRAMS, bad, self.tenant, self.admin)
                self.assertEqual(read_gradebook_rules(self.tenant).moderated_programs, ())

    def test_reading_one_setting_leaves_the_others_at_their_defaults(self):
        set_setting(CFG_RANK_IN_CLASS, False, self.tenant, self.admin)
        rules = read_gradebook_rules(self.tenant, (CFG_SHOW_CLASS_STATS,))
        self.assertTrue(rules.rank_in_class)

    def test_no_tenant_reads_the_defaults(self):
        self.assertEqual(read_gradebook_rules(None), GradebookRules())


class SettingsEndpointTests(GradebookFixture):
    def test_the_body_carries_the_values_and_the_schools_programmes(self):
        response = self.get(self.viewer, "gradebook-settings")
        self.assertEqual(response.status_code, 200)
        data = self.data(response)
        self.assertTrue(data["show_class_stats"])
        self.assertEqual(data["rating_traits"], list(DEFAULT_RATING_TRAITS))
        self.assertEqual(data["moderated_programs"], [])
        self.assertEqual(
            data["programs"],
            [{"code": "JSS", "name": "Junior Secondary", "moderated": False},
             {"code": "SSS", "name": "Senior Secondary", "moderated": False}],
        )

    def test_a_round_trip(self):
        body = {
            "show_class_stats": False, "rank_in_class": False, "conduct_ratings": True,
            "rating_key": False, "rating_traits": ["Punctuality", "Teamwork"],
            "moderated_programs": ["sss"],
        }
        response = self.patch(self.admin, "gradebook-settings", body)
        self.assertEqual(response.status_code, 200, response.data)
        data = self.data(response)
        self.assertEqual(data["moderated_programs"], ["SSS"])
        self.assertEqual(data["rating_traits"], ["Punctuality", "Teamwork"])
        self.assertFalse(data["show_class_stats"])
        self.assertEqual([p["moderated"] for p in data["programs"]], [False, True])
        again = self.data(self.get(self.admin, "gradebook-settings"))
        self.assertEqual(again, data)
        rules = read_gradebook_rules(self.tenant)
        self.assertEqual(rules.moderated_programs, ("SSS",))
        self.assertEqual(read_gradebook_rules(self.solo), GradebookRules())

    def test_a_patch_names_only_what_it_changes(self):
        self.patch(self.admin, "gradebook-settings", {"rank_in_class": False})
        self.patch(self.admin, "gradebook-settings", {"rating_key": False})
        rules = read_gradebook_rules(self.tenant)
        self.assertEqual((rules.rank_in_class, rules.rating_key, rules.show_class_stats), (False, False, True))

    def changes(self):
        return ConfigurationAuditEvent.objects.filter(
            tenant=self.tenant, action="config.value.updated",
        )

    def test_saving_what_is_already_in_force_audits_nothing(self):
        self.patch(self.admin, "gradebook-settings", {"rank_in_class": False})
        before = self.changes().count()
        response = self.patch(self.admin, "gradebook-settings", {
            "rank_in_class": False, "show_class_stats": True,
            "moderated_programs": [],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.changes().count(), before)

    def test_a_change_is_audited_by_the_configuration_trail(self):
        self.patch(self.admin, "gradebook-settings", {"rank_in_class": False})
        event = self.changes().get()
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.after_data["value"], False)

    def test_a_code_no_programme_has_is_refused_and_nothing_is_saved(self):
        response = self.patch(self.admin, "gradebook-settings", {
            "rank_in_class": False, "moderated_programs": ["JSS", "XYZ"],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "UNKNOWN_PROGRAM_CODES")
        self.assertIn("no programme with the code XYZ", self.message(response))
        self.assertTrue(read_gradebook_rules(self.tenant).rank_in_class)

    def test_another_schools_programme_code_is_not_accepted(self):
        response = self.patch(self.admin, "gradebook-settings", {"moderated_programs": ["BAS"]})
        self.assertEqual(self.code(response), "UNKNOWN_PROGRAM_CODES")

    def test_the_traits_are_validated(self):
        cases = {
            "none": [],
            "sixteen": [f"Trait {i}" for i in range(16)],
            "duplicate": ["Punctuality", "punctuality"],
            "long": ["x" * 61],
            "blank": [" "],
        }
        for label, traits in cases.items():
            with self.subTest(case=label):
                response = self.patch(self.admin, "gradebook-settings", {"rating_traits": traits})
                self.assertEqual(response.status_code, 400)
        fifteen = [f"Trait {i}" for i in range(15)]
        response = self.patch(self.admin, "gradebook-settings", {"rating_traits": fifteen})
        self.assertEqual(response.status_code, 200)

    def test_wrong_types_are_refused(self):
        for body in (
            {"rank_in_class": "maybe"}, {"rating_traits": "Punctuality"},
            {"moderated_programs": "JSS"},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.patch(self.admin, "gradebook-settings", body).status_code, 400)

    def test_reading_needs_the_view_key_and_writing_the_update_key(self):
        for user in (self.nobody, self.updater, self.eze):
            self.assertEqual(self.get(user, "gradebook-settings").status_code, 403)
        for user in (self.nobody, self.viewer, self.eze):
            response = self.patch(user, "gradebook-settings", {"rank_in_class": False})
            self.assertEqual(response.status_code, 403)
        self.assertTrue(read_gradebook_rules(self.tenant).rank_in_class)

    def test_a_branch_bound_caller_reads_but_cannot_change_a_school_wide_setting(self):
        self.assertEqual(self.get(self.lekki_admin, "gradebook-settings").status_code, 200)
        response = self.patch(self.lekki_admin, "gradebook-settings", {"rank_in_class": False})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "SHARED_RECORD_READ_ONLY")
        self.assertTrue(read_gradebook_rules(self.tenant).rank_in_class)

    def test_a_school_with_one_branch_changes_its_own_settings(self):
        response = self.patch(self.solo_admin, "gradebook-settings", {
            "moderated_programs": ["bas"],
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["moderated_programs"], ["BAS"])
        self.assertEqual(read_gradebook_rules(self.tenant).moderated_programs, ())


class TellEveryParentTests(GradebookFixture):
    """Who is told of a released result: the primary guardian alone unless the school widens it."""

    def test_every_database_declares_it_off_at_school_scope_as_the_catalogue_does(self):
        row = ConfigurationDefinition.objects.get(key=CFG_TELL_EVERY_PARENT)
        self.assertEqual((row.default_value, sorted(row.allowed_scopes)), (False, ["platform", "school"]))
        [seeded] = [r for r in SCHOOL_SCOPED_DEFINITIONS if r[0] == CFG_TELL_EVERY_PARENT]
        definition = migration_0004.DEFINITION
        self.assertEqual(
            seeded[1:],
            (definition["label"], definition["description"], definition["value_type"],
             definition["default_value"], definition["validation_rules"]),
        )

    def test_a_school_starts_on_the_primary_guardian_alone(self):
        self.assertFalse(read_gradebook_rules(self.tenant).tell_every_parent)
        self.assertFalse(self.data(self.get(self.viewer, "gradebook-settings"))["tell_every_parent"])

    def test_a_school_widens_it_for_itself_and_no_other(self):
        response = self.patch(self.admin, "gradebook-settings", {"tell_every_parent": True})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.data(response)["tell_every_parent"])
        self.assertTrue(read_gradebook_rules(self.tenant).tell_every_parent)
        self.assertFalse(read_gradebook_rules(self.solo).tell_every_parent)

    def test_a_branch_bound_caller_cannot_widen_it(self):
        response = self.patch(self.lekki_admin, "gradebook-settings", {"tell_every_parent": True})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(read_gradebook_rules(self.tenant).tell_every_parent)

    def test_a_value_that_is_not_true_or_false_is_refused(self):
        response = self.patch(self.admin, "gradebook-settings", {"tell_every_parent": "sometimes"})
        self.assertEqual(response.status_code, 400)
