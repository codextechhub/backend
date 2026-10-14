"""The assessment setup screen and its options are found by the search bar."""
from __future__ import annotations

from django.test import TestCase

from vs_search.registry import ACTION, SETTING, places

from ..search_places import CAPABILITY, SETUP_SCREEN
from ..services.configuration import KEY_FOR_FIELD


class SearchPlaceTests(TestCase):
    def test_assessment_setup_is_a_place_guarded_by_the_setup_view_key(self):
        [place] = [p for p in places() if p.key == SETUP_SCREEN]
        self.assertEqual(place.kind, ACTION)
        self.assertEqual(place.label, "Assessment setup")
        self.assertEqual(place.permission, "gradebook.setup.view")
        self.assertEqual(place.capability, CAPABILITY)
        self.assertEqual(place.capability, "gradebook")

    def test_each_option_is_a_setting_place_on_that_screen_anchored_at_its_field(self):
        mine = {p.anchor: p for p in places() if p.kind == SETTING and p.screen == SETUP_SCREEN}
        self.assertEqual(set(mine), set(KEY_FOR_FIELD))
        for place in mine.values():
            self.assertEqual(place.permission, "gradebook.setup.view")
            self.assertEqual(place.capability, "gradebook")
            self.assertEqual(place.group, "Assessment setup")
        self.assertEqual(mine["rank_in_class"].label, "Position In Class")


class ScreenPlaceTests(TestCase):
    def test_the_sheet_hub_and_moderation_screens_are_places_with_their_keys(self):
        from ..search_places import HUB_SCREEN, MODERATION_SCREEN, SHEETS_SCREEN

        expected = {
            SHEETS_SCREEN: ("Score sheets", "gradebook.sheet.view"),
            HUB_SCREEN: ("Results hub", "gradebook.hub.view"),
            MODERATION_SCREEN: ("Moderation", "gradebook.moderation.approve"),
        }
        found = {p.key: p for p in places() if p.key in expected}
        self.assertEqual(set(found), set(expected))
        for key, (label, permission) in expected.items():
            place = found[key]
            self.assertEqual((place.kind, place.label), (ACTION, label))
            self.assertEqual(place.permission, permission)
            self.assertEqual(place.capability, "gradebook")
            self.assertEqual(place.screen, key)


class ClassScreenPlaceTests(TestCase):
    def test_the_broadsheet_approvals_and_fee_holds_are_places_with_their_keys(self):
        from ..search_places import APPROVALS_SCREEN, BROADSHEET_SCREEN, HOLDS_SCREEN

        expected = {
            BROADSHEET_SCREEN: ("Broadsheet", "gradebook.class.submit"),
            APPROVALS_SCREEN: ("Approvals and publishing", "gradebook.class.publish"),
            HOLDS_SCREEN: ("Fee holds", "gradebook.hold.view"),
        }
        found = {p.key: p for p in places() if p.key in expected}
        self.assertEqual(set(found), set(expected))
        for key, (label, permission) in expected.items():
            place = found[key]
            self.assertEqual((place.kind, place.label), (ACTION, label))
            self.assertEqual(place.permission, permission)
            self.assertEqual(place.capability, "gradebook")
            self.assertEqual(place.screen, key)


class ReleaseScreenPlaceTests(TestCase):
    def test_report_cards_and_release_is_a_place_guarded_by_the_release_view_key(self):
        from ..search_places import RELEASE_SCREEN

        [place] = [p for p in places() if p.key == RELEASE_SCREEN]
        self.assertEqual((place.kind, place.label), (ACTION, "Report cards and release"))
        self.assertEqual(place.permission, "gradebook.release.view")
        self.assertEqual(place.capability, "gradebook")
        self.assertEqual(place.screen, RELEASE_SCREEN)

    def test_a_students_results_tab_is_not_a_place_because_a_student_tab_has_no_search_entry(self):
        keys = {p.key for p in places() if p.key.startswith("gradebook.")}
        self.assertNotIn("gradebook.student_results", keys)


class AmendmentScreenPlaceTests(TestCase):
    def test_amendments_is_a_place_any_amendment_key_opens(self):
        from ..search_places import AMENDMENTS_SCREEN

        [place] = [p for p in places() if p.key == AMENDMENTS_SCREEN]
        self.assertEqual((place.kind, place.label), (ACTION, "Amendments"))
        self.assertEqual(
            place.permission_keys, ("gradebook.amendment.create", "gradebook.amendment.approve"),
        )
        self.assertEqual(place.capability, "gradebook")
        self.assertEqual(place.screen, AMENDMENTS_SCREEN)


class AnalyticsScreenPlaceTests(TestCase):
    def test_performance_analytics_is_a_place_guarded_by_the_analytics_view_key(self):
        from ..search_places import ANALYTICS_SCREEN

        [place] = [p for p in places() if p.key == ANALYTICS_SCREEN]
        self.assertEqual((place.kind, place.label), (ACTION, "Performance analytics"))
        self.assertEqual(place.permission, "gradebook.analytics.view")
        self.assertEqual(place.capability, "gradebook")
        self.assertEqual(place.screen, ANALYTICS_SCREEN)
