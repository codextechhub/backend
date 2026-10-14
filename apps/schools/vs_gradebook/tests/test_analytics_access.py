"""Who may open performance analytics, and which classes their branches let them read.

Brightfield (Lekki and Ikeja) and Sunrise (one branch) are built in
:mod:`.analytics_base`. Every refusal here is a status code and nothing else is
checked: the figures are in :mod:`.test_analytics`.
"""
from __future__ import annotations

from .analytics_base import AnalyticsFixture, snapshot
from ..constants import StoredStage


class KeyTests(AnalyticsFixture):
    def test_a_caller_without_the_view_key_is_refused(self):
        for user in (self.nobody, self.hub_reader, self.exporter_only, self.admin):
            with self.subTest(user=user.email):
                self.assertEqual(self.analytics(user).status_code, 403)

    def test_a_caller_without_the_export_key_is_refused_the_file(self):
        for user in (self.nobody, self.viewer_only, self.hub_reader):
            with self.subTest(user=user.email):
                self.assertEqual(self.export(user, table="subjects").status_code, 403)

    def test_the_view_key_alone_opens_the_screen_and_the_export_key_alone_the_file(self):
        self.assertEqual(self.analytics(self.viewer_only).status_code, 200)
        self.assertEqual(self.export(self.exporter_only, table="subjects").status_code, 200)

    def test_an_anonymous_caller_is_refused(self):
        from django.urls import reverse
        from rest_framework.test import APIClient

        self.assertIn(APIClient().get(reverse("gradebook-analytics")).status_code, (401, 403))


class NotFoundTests(AnalyticsFixture):
    def test_a_branch_that_does_not_exist_or_belongs_to_another_school_is_404(self):
        for value in ("999999", str(self.solo_branch.pk), "abc"):
            for call in (
                lambda v: self.analytics(self.reader, branch=v),
                lambda v: self.export(self.reader, table="subjects", branch=v),
            ):
                with self.subTest(branch=value):
                    self.assertEqual(call(value).status_code, 404)

    def test_a_branch_the_reader_cannot_see_is_404_like_one_that_does_not_exist(self):
        self.assertEqual(self.analytics(self.lekki_reader, branch=self.ikeja.pk).status_code, 404)
        self.assertEqual(
            self.export(self.lekki_reader, table="subjects", branch=self.ikeja.pk).status_code,
            404,
        )

    def test_a_term_of_another_school_or_that_does_not_exist_is_404(self):
        for value in (str(self.solo_term.pk), "999999", "abc"):
            with self.subTest(term=value):
                self.assertEqual(self.analytics(self.reader, term=value).status_code, 404)
                self.assertEqual(
                    self.export(self.reader, table="movers", term=value).status_code, 404,
                )

    def test_the_other_schools_published_results_never_appear_here(self):
        self.put(self.solo_class, self.solo_term, [
            (self.solo_pupil, snapshot(
                self.solo_pupil, self.solo_class, self.solo_term, [(self.solo_maths, 77)],
                average=77, position=1,
            )),
        ], branch=self.solo_branch)
        body = self.data(self.analytics(self.reader))
        self.assertNotIn("Basic 3", self.texts(body))
        self.assertEqual({row["class"]["name"] for row in body["subjects"]}, {
            "JSS1 A", "JSS1 B", "JSS1 C",
        })


class BranchScopeTests(AnalyticsFixture):
    def classes(self, body):
        return {row["name"] for row in body["scope"]["classes"]}

    def test_a_school_wide_reader_sees_every_published_class(self):
        body = self.data(self.analytics(self.reader))
        self.assertEqual(self.classes(body), {"JSS1 A", "JSS1 B", "JSS1 C"})
        self.assertEqual(body["scope"]["published_count"], 3)

    def test_a_branch_pinned_reader_sees_only_their_branchs_classes(self):
        lekki = self.data(self.analytics(self.lekki_reader))
        ikeja = self.data(self.analytics(self.ikeja_reader))
        self.assertEqual(self.classes(lekki), {"JSS1 A", "JSS1 B"})
        self.assertEqual(self.classes(ikeja), {"JSS1 C"})
        self.assertEqual({row["class"]["name"] for row in ikeja["subjects"]}, {"JSS1 C"})

    def test_a_subject_is_compared_only_across_the_classes_the_reader_can_see(self):
        [lekki] = self.data(self.analytics(self.lekki_reader))["compare"]
        self.assertEqual([arm["class"]["name"] for arm in lekki["arms"]], ["JSS1 A", "JSS1 B"])
        self.assertEqual(lekki["gap"], "12.50")
        self.assertEqual(self.data(self.analytics(self.ikeja_reader))["compare"], [])

    def test_a_branch_pinned_readers_movers_and_tops_are_their_own_students(self):
        body = self.data(self.analytics(self.ikeja_reader))
        self.assertEqual([row["student"]["name"] for row in body["movers"]], ["Sade Ojo"])
        self.assertEqual([row["class"]["name"] for row in body["tops"]], ["JSS1 C"])

    def test_a_school_wide_reader_can_narrow_to_one_branch(self):
        body = self.data(self.analytics(self.reader, branch=self.ikeja.pk))
        self.assertEqual(self.classes(body), {"JSS1 C"})
        self.assertEqual(self.data(self.analytics(self.reader, branch=self.lekki.pk))["compare"][0]["gap"], "12.50")

    def test_a_branch_pinned_reader_may_name_their_own_branch(self):
        body = self.data(self.analytics(self.lekki_reader, branch=self.lekki.pk))
        self.assertEqual(self.classes(body), {"JSS1 A", "JSS1 B"})

    def test_a_class_with_no_branch_at_a_school_with_several_is_not_here(self):
        self.put(self.shared, self.first_term, [
            (self.sade, snapshot(self.sade, self.shared, self.first_term, [(self.maths, 10)], average=10)),
        ], branch=self.lekki)
        self.assertNotIn("JSS1 Shared", self.classes(self.data(self.analytics(self.reader))))

    def test_the_branch_names_the_classes_only_where_the_school_has_several(self):
        brightfield = self.data(self.analytics(self.reader))
        self.assertTrue(brightfield["scope"]["branch_dimension"])
        self.assertEqual(
            {row["name"]: row["branch"]["name"] for row in brightfield["scope"]["classes"]},
            {"JSS1 A": "Lekki Branch", "JSS1 B": "Lekki Branch", "JSS1 C": "Ikeja Branch"},
        )

    def test_a_school_with_one_branch_carries_no_branch_anywhere(self):
        self.put(self.solo_class, self.solo_term, [
            (self.solo_pupil, snapshot(
                self.solo_pupil, self.solo_class, self.solo_term, [(self.solo_maths, 77)],
                average=77, position=1,
            )),
        ], branch=self.solo_branch)
        body = self.data(self.analytics(self.solo_reader))
        self.assertFalse(body["scope"]["branch_dimension"])
        self.assertEqual(body["scope"]["classes"], [
            {"id": self.solo_class.pk, "name": "Basic 3", "branch": None},
        ])
        self.assertEqual(body["subjects"][0]["average"], "77.00")


class PublishedOnlyTests(AnalyticsFixture):
    def test_a_class_awaiting_the_principal_contributes_nothing(self):
        body = self.data(self.analytics(self.reader))
        self.assertNotIn("SSS1 A", {row["name"] for row in body["scope"]["classes"]})
        self.assertNotIn("SSS1 A", {row["class"]["name"] for row in body["subjects"]})
        self.assertNotIn("Zainab Aliyu", self.texts(body))

    def test_a_class_still_being_entered_contributes_nothing(self):
        self.put(self.shared, self.first_term, [], stage=None, branch=self.lekki)
        self.assertEqual(self.data(self.analytics(self.reader))["scope"]["published_count"], 3)

    def test_a_class_leaves_the_figures_the_moment_its_stage_is_not_published(self):
        from ..models import ClassTerm

        ClassTerm.objects.filter(school_class=self.jss1b, term=self.first_term).update(
            stage=StoredStage.AWAITING,
        )
        body = self.data(self.analytics(self.reader))
        self.assertEqual({row["class"]["name"] for row in body["subjects"]}, {"JSS1 A", "JSS1 C"})
        self.assertEqual(body["scope"]["published_count"], 2)
