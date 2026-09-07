"""Four keys that used to gate two things each, and now gate one apiece.

Each of these keys covered a feature the price list sells at one depth and a
second feature it sells a depth deeper. While they shared a key, neither could
be sold as designed: banding it shallow gave the deeper feature away, and
banding it deep took the shallower one with it.

The tests pin both halves of every split, because only one of them is obvious.
The obvious half is that the new key exists and the deep feature now asks for
it. The half that actually breaks things is the other one: a split silently
removes access from everybody holding the old key, so a migration copies every
grant across, and the old key must go on governing what it always governed.

There is no fixture shortcut here. Each split is checked against the seeded
registry rather than against a hand-made permission, because the thing being
tested is that the seeder and the views agree on a key name.
"""
from django.test import TestCase

from ..models import Permission


SPLITS = [
    # (new key, the key it was split out of, what the new one governs)
    ("academics.exam.view", "academics.timetable.view", "exams"),
    ("academics.exam.create", "academics.timetable.create", "exams"),
    ("academics.exam.update", "academics.timetable.update", "exams"),
    ("academics.exam.manage", "academics.timetable.manage", "exams"),
    ("academics.exam.publish", "academics.timetable.publish", "exams"),
    ("school.staff_records.view", "school.teachers.view", "staff records"),
    ("school.staff_records.update", "school.teachers.update", "staff records"),
    ("school.students.promote", "school.students.manage", "promotion"),
    ("procurement.analytics.view", "procurement.report.view", "spend analysis"),
]


class SplitKeysAreSeededTests(TestCase):
    """Every new key is in the registry, and every old one is still there."""

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

    def test_no_old_key_was_removed(self):
        # A split adds a key; it never takes one away. The old key goes on
        # governing what it always governed.
        for _, old_key, _ in SPLITS:
            with self.subTest(key=old_key):
                self.assertTrue(
                    Permission.objects.filter(key=old_key, is_active=True).exists(),
                    f"{old_key} was removed rather than split",
                )

    def test_a_split_key_keeps_its_sensitivity(self):
        # Splitting must not quietly make a dangerous action look ordinary.
        for new_key, old_key, _ in SPLITS:
            with self.subTest(key=new_key):
                new = Permission.objects.get(key=new_key)
                old = Permission.objects.get(key=old_key)
                self.assertGreaterEqual(
                    ["NORMAL", "SENSITIVE", "CRITICAL"].index(new.sensitivity_level),
                    ["NORMAL", "SENSITIVE", "CRITICAL"].index(old.sensitivity_level),
                    f"{new_key} is treated as less dangerous than {old_key}",
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
