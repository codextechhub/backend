"""A student's report card, rendered from the snapshot published with the class."""
from __future__ import annotations

import datetime as dt

from django.test import SimpleTestCase

from schools.vs_gradebook.models import (
    ClassTerm,
    GradeBand,
    PrincipalComment,
    RemarkTemplate,
    StudentTermResult,
)
from schools.vs_gradebook.services import reportcards
from schools.vs_schools.models import School

from .card_base import CardFixture


class WordingTests(SimpleTestCase):
    def test_ordinals_follow_english_including_the_teens(self):
        expected = {
            1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", 10: "10th", 11: "11th",
            12: "12th", 13: "13th", 14: "14th", 20: "20th", 21: "21st", 22: "22nd",
            23: "23rd", 24: "24th", 101: "101st", 111: "111th", 112: "112th", 113: "113th",
        }
        for number, word in expected.items():
            with self.subTest(number=number):
                self.assertEqual(reportcards.ordinal(number), word)

    def test_a_position_reads_of_the_roll(self):
        self.assertEqual(reportcards.position_label(3, 24), "3rd of 24")
        self.assertEqual(reportcards.position_label(12, 30), "12th of 30")

    def test_a_date_reads_weekday_day_month_year_without_padding(self):
        self.assertEqual(reportcards.day_label(dt.date(2026, 1, 5)), "Mon 5 Jan 2026")
        self.assertEqual(reportcards.day_label(dt.date(2025, 12, 16)), "Tue 16 Dec 2025")
        self.assertEqual(reportcards.day_label(dt.date(2025, 10, 1)), "Wed 1 Oct 2025")

    def test_the_grading_key_runs_highest_first_with_a_middle_dot(self):
        key = [{"grade": "A", "min_score": 70}, {"grade": "B", "min_score": 60}, {"grade": "F", "min_score": 0}]
        self.assertEqual(reportcards.grading_key_line(key), "A 70+ · B 60+ · F 0+")

    def test_a_cell_prints_the_mark_or_the_marker_in_its_own_words(self):
        self.assertEqual(reportcards._cell_text({"mark": 18, "marker": None}), "18")
        self.assertEqual(reportcards._cell_text({"mark": None, "marker": "ABS"}), "ABS")
        self.assertEqual(reportcards._cell_text({"mark": None, "marker": "EXC"}), "Exc")
        self.assertEqual(reportcards._cell_text({"mark": 0, "marker": None}), "0")


def snapshot(**changes):
    base = {
        "student": {"id": 1, "name": "Tunde Bello", "admission_number": "BFS/002"},
        "class": {"id": 5, "name": "JSS1 A"}, "class_size": 24,
        "term": {"id": 1, "name": "First Term"}, "session": {"id": 1, "name": "2025/2026"},
        "components": [
            {"id": 1, "name": "1st CA", "short_name": "CA1", "max_score": 20},
            {"id": 3, "name": "Exam", "short_name": "EXAM", "max_score": 60},
        ],
        "subjects": [{
            "subject": {"id": 1, "name": "Mathematics"},
            "cells": [{"component": 1, "mark": 18, "marker": None},
                      {"component": 3, "mark": None, "marker": "ABS"}],
            "total": 18, "obtainable": 80, "percentage": "22.50", "grade": "F",
            "remark": "Fail", "is_fail": True,
            "class_average": "63.5", "highest": 79, "lowest": 18,
        }],
        "total_obtained": 18, "total_obtainable": 80, "overall_average": "22.50",
        "position": 3, "roll_size": 24,
        "attendance": {"opened": 60, "present": 55, "absent": 5},
        "teacher_comment": "Good.", "class_teacher": "Chidi Eze",
        "principal_comment": "Work harder.", "principal": "Pauline Principal",
        "grading_key": [{"min_score": 70, "grade": "A"}, {"min_score": 0, "grade": "F"}],
        "next_term_begins": "2026-01-05",
        "options": {"show_class_stats": True, "rating_key": True},
        "ratings": {"Punctuality": 5}, "traits": ["Punctuality", "Neatness"],
    }
    base.update(changes)
    return base


HEAD = {"school_name": "Brightfield", "logo_url": None, "branch_name": None,
        "branch_address": None, "phone": None}


def build(**changes):
    return reportcards.card(snapshot(**changes), letterhead_block=HEAD, held=False, version=1)


class CardBuilderTests(SimpleTestCase):
    def test_the_totals_state_the_sum_the_average_and_the_ranked_position(self):
        totals = {line["label"]: line["value"] for line in build()["totals"]}
        self.assertEqual(totals, {
            "Total obtained": "18", "Total obtainable": "80",
            "Overall average": "22.50%", "Position in class": "3rd of 24",
        })

    def test_there_is_no_position_line_when_the_snapshot_has_no_position(self):
        snap = snapshot()
        del snap["position"], snap["roll_size"]
        card = reportcards.card(snap, letterhead_block=HEAD, held=False, version=1)
        self.assertNotIn("Position in class", [line["label"] for line in card["totals"]])

    def test_class_figures_are_present_to_one_place_only_when_the_school_showed_them(self):
        shown = build()["subjects"][0]
        self.assertEqual((shown["class_average"], shown["highest"], shown["lowest"]), ("63.5", 79, 18))
        hidden = build(options={"show_class_stats": False, "rating_key": True})["subjects"][0]
        for key in ("class_average", "highest", "lowest"):
            self.assertNotIn(key, hidden)

    def test_the_class_average_is_printed_to_one_place_whatever_was_stored(self):
        snap = snapshot()
        snap["subjects"][0]["class_average"] = "63.55"
        card = reportcards.card(snap, letterhead_block=HEAD, held=False, version=1)
        self.assertEqual(card["subjects"][0]["class_average"], "63.6")

    def test_cells_print_marks_and_markers(self):
        self.assertEqual(build()["subjects"][0]["cells"], ["18", "ABS"])

    def test_ratings_and_their_key_are_absent_when_the_school_records_none(self):
        snap = snapshot()
        del snap["ratings"], snap["traits"]
        card = reportcards.card(snap, letterhead_block=HEAD, held=False, version=1)
        self.assertNotIn("ratings", card)
        self.assertNotIn("rating_key", card)

    def test_ratings_list_the_rated_traits_in_the_schools_order_with_the_key(self):
        card = build()
        self.assertEqual(card["ratings"], [{"trait": "Punctuality", "value": 5}])
        self.assertEqual(
            card["rating_key"], "1 poor · 2 fair · 3 good · 4 very good · 5 excellent",
        )

    def test_the_key_is_absent_when_the_school_switched_it_off_though_ratings_print(self):
        card = build(options={"show_class_stats": True, "rating_key": False})
        self.assertIn("ratings", card)
        self.assertNotIn("rating_key", card)

    def test_the_next_term_line_is_worded_and_absent_when_there_is_no_next_term(self):
        self.assertEqual(build()["next_term_line"], "Next term begins Mon 5 Jan 2026")
        self.assertNotIn("next_term_line", build(next_term_begins=None))

    def test_the_lines_the_card_prints(self):
        card = build()
        self.assertEqual(card["term_line"], "Report card · First Term, 2025/2026")
        self.assertEqual(
            [(line["label"], line["value"]) for line in card["facts"]],
            [("Student", "Tunde Bello"), ("Admission no.", "BFS/002"), ("Class", "JSS1 A"),
             ("Number in class", "24")],
        )
        self.assertEqual(
            [(line["label"], line["value"]) for line in card["attendance"]],
            [("Times school opened", "60"), ("Times present", "55"), ("Times absent", "5")],
        )
        self.assertEqual(card["teacher_name"], "Chidi Eze, class teacher")
        self.assertEqual(card["principal_name"], "Pauline Principal")
        self.assertEqual(card["grading_key"], "A 70+ · F 0+")
        self.assertEqual(card["components"], [
            {"short_name": "CA1", "max_score": 20}, {"short_name": "EXAM", "max_score": 60},
        ])

    def test_a_class_with_no_class_teacher_has_no_teacher_name(self):
        self.assertIsNone(build(class_teacher=None)["teacher_name"])


class ReportCardTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        School.objects.filter(tenant=cls.tenant).update(phone="08012345678", address="1 School Road")
        cls.lekki.address = "12 Admiralty Way, Lekki"
        cls.lekki.save()
        cls.publish_jss1a(held=[cls.amina])

    def test_the_card_carries_everything_a_family_is_given(self):
        response = self.report_card(self.releaser, self.tunde)
        self.assertEqual(response.status_code, 200, response.data)
        card = self.data(response)
        self.assertEqual(card["term_line"], "Report card · First Term, 2025/2026")
        self.assertEqual(self.lines(card, "facts"), {
            "Student": "Tunde Bello", "Admission no.": "BFS/002", "Class": "JSS1 A",
            "Number in class": "4",
        })
        self.assertEqual(
            card["components"],
            [{"short_name": "CA1", "max_score": 20}, {"short_name": "CA2", "max_score": 20},
             {"short_name": "EXAM", "max_score": 60}],
        )
        self.assertEqual(card["version"], 1)
        self.assertIs(card["held"], False)

    def test_subjects_print_cells_total_grade_remark_and_the_class_figures(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        subjects = {s["name"]: s for s in card["subjects"]}
        self.assertEqual(set(subjects), {"French", "Mathematics"})
        maths = subjects["Mathematics"]
        self.assertEqual(maths["cells"], ["15", "15", "45"])
        self.assertEqual((maths["total"], maths["grade"], maths["remark"]), (75, "A", "Excellent"))
        self.assertEqual((maths["class_average"], maths["highest"], maths["lowest"]), ("63.5", 79, 40))

    def test_totals_state_the_sum_the_average_and_the_position_of_a_ranking_school(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertEqual(self.lines(card, "totals"), {
            "Total obtained": "150", "Total obtainable": "200",
            "Overall average": "75.00%", "Position in class": "1st of 4",
        })

    def test_attendance_comes_from_the_published_term(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertEqual(self.lines(card, "attendance"), {
            "Times school opened": "60", "Times present": "55", "Times absent": "5",
        })

    def test_ratings_and_the_key_print_for_a_school_that_rates(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertEqual(
            {r["trait"]: r["value"] for r in card["ratings"]}, {"Punctuality": 5, "Sports": 3},
        )
        self.assertIn("5 excellent", card["rating_key"])

    def test_both_comments_are_signed_with_their_authors(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertEqual(card["teacher_comment"], "A pleasant, hardworking student.")
        self.assertEqual(card["teacher_name"], "Chidi Eze, class teacher")
        self.assertEqual(card["principal_comment"], "An excellent result. Keep it up.")
        self.assertEqual(card["principal_name"], "Pauline Principal")

    def test_the_grading_key_and_the_next_term_line(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertEqual(
            card["grading_key"],
            "A 70+ · B 60+ · C 50+ · D 40+ · F 0+",
        )
        self.assertEqual(card["next_term_line"], "Next term begins Mon 12 Jan 2026")

    def test_the_letterhead_is_the_schools_present_identity_with_the_branch_address(self):
        head = self.data(self.report_card(self.releaser, self.tunde))["letterhead"]
        self.assertEqual(head, {
            "school_name": "Brightfield Schools", "logo_url": None,
            "branch_name": "Lekki Branch", "branch_address": "12 Admiralty Way, Lekki",
            "phone": "08012345678",
        })

    def test_a_branch_without_an_address_falls_back_to_the_schools(self):
        self.lekki.address = ""
        self.lekki.save()
        head = self.data(self.report_card(self.releaser, self.tunde))["letterhead"]
        self.assertEqual(head["branch_address"], "1 School Road")

    def test_the_logo_is_a_signed_url_when_the_school_has_one(self):
        from schools.vs_schools.models import SchoolBranding

        school = School.objects.get(tenant=self.tenant)
        SchoolBranding.objects.create(school=school, logo="school_logos/brightfield.png")
        head = self.data(self.report_card(self.releaser, self.tunde))["letterhead"]
        self.assertTrue(head["logo_url"])
        self.assertIn("brightfield.png", head["logo_url"])

    def test_a_held_students_card_is_readable_by_staff_and_says_it_is_held(self):
        response = self.report_card(self.releaser, self.amina)
        self.assertEqual(response.status_code, 200)
        self.assertIs(self.data(response)["held"], True)
        self.assertEqual(self.lines(self.data(response), "totals")["Position in class"], "3rd of 4")

    def test_a_student_the_class_did_not_publish_is_a_404(self):
        self.assertEqual(self.report_card(self.releaser, self.obinna).status_code, 404)
        self.assertEqual(self.report_card(self.releaser, self.sade).status_code, 404)

    def test_a_card_for_a_class_that_is_not_published_is_a_409(self):
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage="AWAITING")
        response = self.report_card(self.releaser, self.tunde)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NOT_PUBLISHED")


class FrozenCardTests(CardFixture):
    """Nothing changed after publication changes a card."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a()

    def test_a_scale_a_remark_template_and_a_mark_changed_afterwards_leave_the_card_as_published(self):
        before = self.data(self.report_card(self.releaser, self.tunde))
        GradeBand.objects.filter(scale=self.junior_scale).update(grade="Z", remark="Changed", min_score=99)
        RemarkTemplate.objects.update(text="Rewritten after publication.")
        PrincipalComment.objects.update(text="Rewritten as well.")
        from schools.vs_gradebook.models import Score

        Score.objects.update(mark=0)
        StudentTermResult.objects.update(teacher_comment="Edited after publication.")
        after = self.data(self.report_card(self.releaser, self.tunde))
        self.assertEqual(after, before)
        self.assertEqual(after["subjects"][1]["grade"], "A")
        self.assertEqual(after["principal_comment"], "An excellent result. Keep it up.")
        self.assertEqual(after["teacher_comment"], "A pleasant, hardworking student.")

    def test_a_setting_changed_afterwards_does_not_add_a_position_the_card_never_had(self):
        from .base import set_setting

        set_setting("gradebook.rank_in_class", False, self.tenant, self.admin)
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertEqual(self.lines(card, "totals")["Position in class"], "1st of 4")

    def test_the_letterhead_alone_is_read_live(self):
        School.objects.filter(tenant=self.tenant).update(phone="09099999999")
        head = self.data(self.report_card(self.releaser, self.tunde))["letterhead"]
        self.assertEqual(head["phone"], "09099999999")


class CardSecurityTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])

    def test_only_the_release_key_may_read_a_card(self):
        for user in (self.result_reader, self.principal, self.eze, self.admin, self.bursar, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.report_card(user, self.tunde).status_code, 403)

    def test_another_branchs_pinned_reader_gets_a_404(self):
        self.assertEqual(self.report_card(self.ikeja_releaser, self.tunde).status_code, 404)
        self.assertEqual(self.report_card(self.lekki_releaser, self.tunde).status_code, 200)

    def test_another_schools_reader_gets_a_404_for_the_class(self):
        response = self.call(
            "get", self.solo_releaser, "gradebook-report-card", class_id=self.jss1a.pk,
            student_id=self.tunde.pk, term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)

    def test_a_term_of_another_school_is_a_404(self):
        self.assertEqual(
            self.report_card(self.releaser, self.tunde, term=self.solo_term).status_code, 404,
        )


class OneBranchSchoolCardTests(CardFixture):
    """Sunrise ranks nobody and records no ratings, so its card prints neither."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_sunrise()

    def card_of(self, pupil):
        response = self.call(
            "get", self.solo_releaser, "gradebook-report-card", class_id=self.solo_class.pk,
            student_id=pupil.pk, term=self.solo_term,
        )
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def test_a_school_that_does_not_rank_has_no_position_line(self):
        card = self.card_of(self.solo_pupil)
        self.assertNotIn("Position in class", self.lines(card, "totals"))
        self.assertIn("Overall average", self.lines(card, "totals"))

    def test_a_school_that_records_no_ratings_has_neither_ratings_nor_a_key(self):
        card = self.card_of(self.solo_pupil)
        self.assertNotIn("ratings", card)
        self.assertNotIn("rating_key", card)

    def test_the_branch_recedes_from_the_letterhead(self):
        head = self.card_of(self.solo_pupil)["letterhead"]
        self.assertIsNone(head["branch_name"])
        self.assertEqual(head["school_name"], "Sunrise Academy")

    def test_another_schools_student_in_a_published_class_is_a_404(self):
        response = self.call(
            "get", self.solo_releaser, "gradebook-report-card", class_id=self.solo_class.pk,
            student_id=self.tunde.pk, term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)


class OptionsAtPublicationTests(CardFixture):
    """A card prints what the school's options said when the class was published."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_academics.models import AcademicTerm

        from .base import set_setting

        set_setting("gradebook.show_class_stats", False, cls.tenant, cls.admin)
        set_setting("gradebook.rating_key", False, cls.tenant, cls.admin)
        AcademicTerm.all_objects.filter(pk=cls.second_term.pk).delete()
        cls.publish_jss1a()

    def test_class_figures_are_absent_when_the_option_was_off_at_publication(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        for subject in card["subjects"]:
            for key in ("class_average", "highest", "lowest"):
                self.assertNotIn(key, subject)
            self.assertIn("total", subject)

    def test_the_key_is_absent_when_it_was_off_though_the_ratings_print(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertTrue(card["ratings"])
        self.assertNotIn("rating_key", card)

    def test_there_is_no_next_term_line_when_the_school_had_no_next_term(self):
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertNotIn("next_term_line", card)

    def test_turning_the_options_on_afterwards_adds_nothing(self):
        from .base import set_setting

        set_setting("gradebook.show_class_stats", True, self.tenant, self.admin)
        card = self.data(self.report_card(self.releaser, self.tunde))
        self.assertNotIn("class_average", card["subjects"][0])
