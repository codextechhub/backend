"""Keys split from broader permissions stay registered at the right risk.

Each of these keys covered a feature the price list sells at one depth and a
second feature it sells a depth deeper. While they shared a key, neither could
be sold as designed: banding it shallow gave the deeper feature away, and
banding it deep took the shallower one with it.

Some source keys remain because they still govern a separate operation. Broad
action keys do not: migration 0027 copies their grants to concrete operations
and removes them.

There is no fixture shortcut here. Each split is checked against the seeded
registry rather than against a hand-made permission, because the thing being
tested is that the seeder and the views agree on a key name.
"""
from django.test import TestCase

from ..models import Permission


SPLITS = [
    # (new key, remaining source key if any, what the new one governs)
    ("academics.exam.view", "academics.timetable.view", "exams"),
    ("academics.exam.create", "academics.timetable.create", "exams"),
    ("academics.exam.update", "academics.timetable.update", "exams"),
    ("academics.exam.delete", None, "exam deletion"),
    ("academics.exam.publish", "academics.timetable.publish", "exams"),
    ("school.staff_records.view", "school.teachers.view", "staff records"),
    ("school.staff_records.update", "school.teachers.update", "staff records"),
    ("school.students.promote", None, "promotion"),
    ("procurement.analytics.view", "procurement.report.view", "spend analysis"),
]


class SplitKeysAreSeededTests(TestCase):
    """Every new key exists and only useful source keys remain."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_actions", verbosity=0)
        call_command("seed_school_permissions", verbosity=0)
        call_command("seed_procurement_permissions", verbosity=0)

    def test_every_new_key_exists(self):
        for new_key, _, governs in SPLITS:
            with self.subTest(key=new_key):
                self.assertTrue(
                    Permission.objects.filter(key=new_key, is_active=True).exists(),
                    f"{new_key} governs {governs} and is not in the registry",
                )

    def test_source_keys_with_a_remaining_operation_stay_registered(self):
        for _, old_key, _ in SPLITS:
            if old_key is None:
                continue
            with self.subTest(key=old_key):
                self.assertTrue(
                    Permission.objects.filter(key=old_key, is_active=True).exists(),
                    f"{old_key} still governs an operation and was removed",
                )

    def test_a_split_key_is_not_less_sensitive_than_its_source(self):
        for new_key, old_key, _ in SPLITS:
            if old_key is None:
                continue
            with self.subTest(key=new_key):
                new = Permission.objects.get(key=new_key)
                old = Permission.objects.get(key=old_key)
                self.assertGreaterEqual(
                    ["NORMAL", "SENSITIVE", "CRITICAL"].index(new.sensitivity_level),
                    ["NORMAL", "SENSITIVE", "CRITICAL"].index(old.sensitivity_level),
                    f"{new_key} is treated as less dangerous than {old_key}",
                )

    def test_concrete_delete_and_promotion_keys_are_sensitive(self):
        for key in ("academics.exam.delete", "school.students.promote"):
            with self.subTest(key=key):
                self.assertEqual(
                    Permission.objects.get(key=key).sensitivity_level,
                    Permission.Sensitivity.SENSITIVE,
                )

    def test_broad_source_keys_are_gone(self):
        self.assertFalse(
            Permission.objects.filter(
                key__in=(
                    "academics.exam.manage",
                    "academics.timetable.manage",
                    "school.students.manage",
                ),
            ).exists(),
        )

    def test_the_promote_action_is_in_the_canonical_vocabulary(self):
        from ..models import PermissionAction

        self.assertTrue(PermissionAction.objects.filter(name="promote").exists())


class ViewsAskForTheSplitKeyTests(TestCase):
    """The views moved with the keys, which is the point of splitting them."""

    def test_exam_views_ask_for_the_exam_key(self):
        from schools.vs_calendar.views import exams

        source = open(exams.__file__.replace(".pyc", ".py")).read()
        self.assertIn("PERM_EXAM_VIEW", source)
        self.assertNotIn(
            "PERM_TIMETABLE", source,
            "exams still borrow a timetable key, so the two cannot be banded apart",
        )

    def test_staff_record_views_ask_for_the_record_key(self):
        from schools.vs_staff.views import records

        source = open(records.__file__.replace(".pyc", ".py")).read()
        self.assertIn("PERM_RECORDS_VIEW", source)
        self.assertIn("PERM_RECORDS_UPDATE", source)

    def test_promotion_views_ask_for_the_promote_key(self):
        from schools.vs_students.views import promotion

        source = open(promotion.__file__.replace(".pyc", ".py")).read()
        self.assertIn("PERM_PROMOTE", source)
        self.assertNotIn(
            "PERM_MANAGE", source,
            "promotion still rides on the key that transfers one child",
        )

    def test_the_analytical_reports_moved_and_the_insights_did_not(self):
        from vs_procurement.views import catalog, reports, vendors

        analytical = open(reports.__file__.replace(".pyc", ".py")).read()
        self.assertIn("procurement.analytics.view", analytical)
        self.assertNotIn("procurement.report.view", analytical)

        # The shallow end stays where it was: a category or catalogue insight
        # is sold a depth above spend analysis, not with it.
        shallow = open(catalog.__file__.replace(".pyc", ".py")).read()
        self.assertIn("procurement.report.view", shallow)
        vendor_source = open(vendors.__file__.replace(".pyc", ".py")).read()
        self.assertIn("procurement.report.view", vendor_source)
        self.assertIn("procurement.analytics.view", vendor_source)
