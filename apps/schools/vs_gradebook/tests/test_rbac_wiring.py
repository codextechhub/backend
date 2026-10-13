"""The gradebook's place in the permission, capability and price-list tables."""
from __future__ import annotations

from django.test import SimpleTestCase

from vs_rbac.capability_map import capability_for
from vs_rbac.permission_bands import CORE, band_for, capability_key_for

from .. import constants

KEYS = (
    constants.PERM_HUB_VIEW, constants.PERM_SHEET_VIEW, constants.PERM_SHEET_ENTER,
    constants.PERM_SHEET_ENTER_ANY, constants.PERM_SHEET_REMIND,
    constants.PERM_MODERATION_APPROVE, constants.PERM_CLASS_SUBMIT,
    constants.PERM_CLASS_PUBLISH, constants.PERM_HOLD_VIEW, constants.PERM_HOLD_UPDATE,
    constants.PERM_RELEASE_VIEW, constants.PERM_RESULT_VIEW, constants.PERM_SETUP_VIEW,
    constants.PERM_SETUP_UPDATE,
)


class RbacWiringTests(SimpleTestCase):
    def test_every_key_is_in_the_gradebook_module(self):
        self.assertEqual(len(set(KEYS)), 14)
        for key in KEYS:
            with self.subTest(key=key):
                self.assertEqual(key.count("."), 2)
                self.assertTrue(key.startswith("gradebook."))

    def test_the_module_answers_to_the_gradebook_capability(self):
        for resource in ("hub", "sheet", "moderation", "class", "hold", "release", "result", "setup"):
            self.assertEqual(capability_for("gradebook", resource), "gradebook")

    def test_every_key_sits_in_the_gradebook_core_band(self):
        for key in KEYS:
            module, resource, action = key.split(".")
            with self.subTest(key=key):
                self.assertEqual(band_for(module, resource, action), CORE)
                self.assertEqual(capability_key_for(module, resource, action), "gradebook_core")
