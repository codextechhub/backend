"""A pupil's own results, report card and school year in the portal.

Brightfield publishes JSS1 A's First Term with Amina's result held for fees.
Chiamaka reads her released result, with each subject's teacher and the class
figures the school shows; Amina is told her result is withheld and sees no
figure and no card. The cumulative case publishes the whole year and reads it
back from the last term.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_gradebook.models import StudentTermResult
from schools.vs_gradebook.tests.card_base import CardFixture
from schools.vs_gradebook.tests.cumulative_base import CumulativeFixture
from schools.vs_students.services import portal_access
from vs_user.models import User


def give_access(pupil, actor):
    """Give *pupil* a working portal account and return it."""
    if not (pupil.student_number or pupil.email):
        pupil.student_number = f"BFS/{pupil.pk:04d}"
        pupil.save(update_fields=["student_number"])
    if not pupil.email:
        pupil.email = f"{pupil.first_name.lower()}.{pupil.pk}@example.ng"
        pupil.save(update_fields=["email"])
    portal_access.issue(pupil, actor=actor)
    pupil.refresh_from_db()
    User.objects.filter(pk=pupil.user_id).update(status=User.Status.ACTIVE, is_active=True)
    return User.objects.get(pk=pupil.user_id)


def read(user, name, **params):
    return TenantAPIClient(user).get(reverse(name), params)


class TermResultsTests(CardFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_jss1a(held=[cls.amina])
        cls.chiamaka_user = give_access(cls.chiamaka, cls.principal)
        cls.amina_user = give_access(cls.amina, cls.principal)

    def test_a_released_term_carries_each_subject_with_its_teacher(self):
        data = read(self.chiamaka_user, "student-portal-results").data["data"]
        self.assertEqual(data["state"], "released")
        self.assertEqual(data["term"]["id"], self.first_term.pk)
        maths = next(s for s in data["subjects"] if s["name"] == self.maths.name)
        self.assertTrue(maths["teacher"])
        self.assertEqual(len(maths["cells"]), len(data["components"]))
        self.assertIn("overall_average", data["summary"])

    def test_the_snapshot_records_the_teacher_and_follows_the_class_figures_rule(self):
        snap = StudentTermResult.objects.get(student=self.chiamaka).snapshot
        self.assertIn("teacher", snap["subjects"][0]["subject"])
        shows = snap["options"]["show_class_stats"]
        self.assertEqual("class_average" in snap, bool(shows))
        data = read(self.chiamaka_user, "student-portal-results").data["data"]
        self.assertEqual("class_average" in data["summary"], bool(shows))

    def test_a_held_result_is_withheld_with_no_figures(self):
        data = read(self.amina_user, "student-portal-results").data["data"]
        self.assertEqual(data["state"], "withheld")
        self.assertIn("ask the school office", data["message"].lower())
        for hidden in ("subjects", "summary", "teacher_comment"):
            self.assertNotIn(hidden, data)

    def test_a_held_card_cannot_be_opened(self):
        response = read(self.amina_user, "student-portal-report-card", term=self.first_term.pk)
        self.assertEqual(response.status_code, 404)

    def test_a_released_card_is_the_gradebooks_card(self):
        response = read(self.chiamaka_user, "student-portal-report-card", term=self.first_term.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["held"])
        self.assertIn("subjects", response.data["data"])

    def test_a_card_needs_a_term(self):
        self.assertEqual(read(self.chiamaka_user, "student-portal-report-card").status_code, 404)

    def test_a_term_with_nothing_published_says_so(self):
        data = read(self.chiamaka_user, "student-portal-results", term=self.second_term.pk).data["data"]
        self.assertEqual(data["state"], "not_published")

    def test_another_schools_term_is_not_found(self):
        response = read(self.chiamaka_user, "student-portal-results", term=self.solo_term.pk)
        self.assertEqual(response.status_code, 404)

    def test_staff_cannot_read_the_portal_results(self):
        self.assertEqual(read(self.principal, "student-portal-results").status_code, 404)

    def test_the_held_reason_never_reaches_the_pupil(self):
        response = read(self.amina_user, "student-portal-results")
        self.assertNotIn("Fees outstanding", str(response.data))


class SessionResultsTests(CumulativeFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.publish_year()
        cls.chiamaka_user = give_access(cls.chiamaka, cls.principal)

    def test_the_year_lists_each_term_and_every_subject_across_it(self):
        data = read(self.chiamaka_user, "student-portal-results-session").data["data"]
        self.assertEqual(data["state"], "released")
        self.assertEqual(data["terms_released"], len(data["terms"]))
        cumulative = data["cumulative"]
        self.assertIsNotNone(cumulative)
        self.assertEqual(len(cumulative["subjects"][0]["terms"]), len(cumulative["terms"]))
        self.assertIn("annual_average", cumulative)
        self.assertIn("remark", cumulative)

    def test_the_year_says_how_many_terms_it_has_and_when_each_was_released(self):
        data = read(self.chiamaka_user, "student-portal-results-session").data["data"]
        self.assertEqual(data["terms_total"], 3)
        for term in data["terms"]:
            self.assertIsNotNone(term["released_at"])

    def test_each_subject_carries_its_annual_grade(self):
        cumulative = read(self.chiamaka_user, "student-portal-results-session").data["data"]["cumulative"]
        graded = [row for row in cumulative["subjects"] if row["annual_average"] is not None]
        self.assertTrue(graded)
        for row in graded:
            self.assertTrue(row["annual_grade"], row)

    def test_the_class_average_for_the_year_is_the_mean_of_its_terms(self):
        from decimal import Decimal

        data = read(self.chiamaka_user, "student-portal-results-session").data["data"]
        figures = [Decimal(str(t["class_average"])) for t in data["terms"]]
        expected = (sum(figures) / len(figures)).quantize(Decimal("0.01"))
        self.assertEqual(data["cumulative"]["class_average"], str(expected))

    def test_a_school_that_shows_no_class_figures_shows_no_class_average(self):
        for result in StudentTermResult.objects.filter(student=self.chiamaka):
            snapshot = dict(result.snapshot)
            snapshot.pop("class_average", None)
            StudentTermResult.objects.filter(pk=result.pk).update(snapshot=snapshot)
        data = read(self.chiamaka_user, "student-portal-results-session").data["data"]
        self.assertNotIn("class_average", data["cumulative"])

    def test_a_pupil_with_nothing_published_reads_an_empty_year(self):
        give = give_access(self.kelechi, self.principal)
        StudentTermResult.objects.filter(student=self.kelechi).update(snapshot=None)
        data = read(give, "student-portal-results-session").data["data"]
        self.assertEqual(data["state"], "not_published")
