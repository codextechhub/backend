"""The gradebook's notification events and what their copy may say."""
from __future__ import annotations

import re

from django.test import SimpleTestCase

from vs_notifications.constants import EVENT_TYPE_REGISTRY, ChannelChoices
from vs_notifications.services.render import render_template
from vs_notifications.services.seed import _build_default_templates

from .. import constants

KEYS = {
    constants.EVENT_SHEET_REMINDER: {ChannelChoices.IN_APP, ChannelChoices.EMAIL},
    constants.EVENT_SHEET_RETURNED: {ChannelChoices.IN_APP},
    constants.EVENT_CLASS_RETURNED: {ChannelChoices.IN_APP},
    constants.EVENT_REPORT_CARD_READY: {ChannelChoices.IN_APP, ChannelChoices.EMAIL, ChannelChoices.PUSH},
    constants.EVENT_REPORT_CARD_UPDATED: {ChannelChoices.IN_APP, ChannelChoices.EMAIL},
    constants.EVENT_RESULT_RELEASED: {ChannelChoices.IN_APP, ChannelChoices.PUSH},
}
GUARDIAN_EVENTS = (constants.EVENT_REPORT_CARD_READY, constants.EVENT_REPORT_CARD_UPDATED)

#: The only values a guardian's report card notice may be built from.
ALLOWED_GUARDIAN_VARIABLES = {
    "student_first_name", "term_name", "session_name", "tenant_name", "branch_name",
    "tenant_phone", "email_brand",
}
_VARIABLE = re.compile(r"\{\{\s*([a-z_0-9]+)|\{%\s*if\s+([a-z_0-9]+)")


class RegistryTests(SimpleTestCase):
    def entries(self):
        return {e["key"]: e for e in EVENT_TYPE_REGISTRY if e["key"] in KEYS}

    def test_every_gradebook_event_is_registered_with_its_channels(self):
        entries = self.entries()
        self.assertEqual(set(entries), set(KEYS))
        for key, channels in KEYS.items():
            with self.subTest(event=key):
                self.assertEqual(set(entries[key]["supported_channels"]), channels)
                self.assertEqual(entries[key]["source_module"], "vs_gradebook")
                self.assertTrue(entries[key]["branch_scoped"])
                self.assertTrue(entries[key].get("is_active", True))

    def test_every_supported_channel_has_default_copy_and_no_other_does(self):
        templates = _build_default_templates()
        for key, channels in KEYS.items():
            with self.subTest(event=key):
                self.assertEqual({c for (k, c) in templates if k == key}, channels)

    def test_a_guardians_notice_carries_nothing_from_the_result(self):
        templates = _build_default_templates()
        for key in GUARDIAN_EVENTS:
            for channel in KEYS[key]:
                with self.subTest(event=key, channel=channel):
                    text = templates[(key, channel)]["subject"] + templates[(key, channel)]["body"]
                    used = {a or b for a, b in _VARIABLE.findall(text)}
                    self.assertLessEqual(used, ALLOWED_GUARDIAN_VARIABLES)
                    self.assertNotRegex(text.lower(), r"https?://|attach|score|grade|position|comment")

    def test_the_pupils_notice_names_the_term_and_nothing_from_the_result(self):
        templates = _build_default_templates()
        for channel in KEYS[constants.EVENT_RESULT_RELEASED]:
            with self.subTest(channel=channel):
                entry = templates[(constants.EVENT_RESULT_RELEASED, channel)]
                text = entry["subject"] + entry["body"]
                used = {a or b for a, b in _VARIABLE.findall(text)}
                self.assertLessEqual(used, {"term_name", "session_name", "tenant_name"})
                self.assertNotRegex(text.lower(), r"score|grade|position|average|%")

    def test_the_guardian_email_says_the_card_is_ready_to_collect_from_the_school(self):
        body = _build_default_templates()[(constants.EVENT_REPORT_CARD_READY, ChannelChoices.EMAIL)]["body"]
        rendered = render_template(body, {
            "student_first_name": "Tunde", "term_name": "First Term", "session_name": "2025/2026",
            "tenant_name": "Brightfield Schools", "branch_name": "Lekki Branch",
            "tenant_phone": "0801 234 5678", "email_brand": "Vision",
        })
        self.assertIn("Tunde's report card for First Term, 2025/2026 is ready to collect from the school.", rendered)
        self.assertIn("0801 234 5678", rendered)
        self.assertIn("Brightfield Schools, Lekki Branch", rendered)

    def test_the_guardian_email_leaves_out_what_a_school_cannot_supply(self):
        body = _build_default_templates()[(constants.EVENT_REPORT_CARD_READY, ChannelChoices.EMAIL)]["body"]
        rendered = render_template(body, {
            "student_first_name": "Tunde", "term_name": "First Term", "session_name": "2025/2026",
            "tenant_name": "Sunrise Academy", "branch_name": "", "tenant_phone": "",
            "email_brand": "Vision",
        })
        self.assertNotIn("You can reach", rendered)
        self.assertIn("Sunrise Academy via Vision", rendered)
